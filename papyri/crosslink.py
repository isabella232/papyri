from __future__ import annotations

import builtins
import json
import logging
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple, Any

from rich.logging import RichHandler
import cbor2
from there import print

from .config import ingest_dir
from .gen import DocBlob, normalise_ref
from .graphstore import GraphStore, Key
from .take2 import (
    Node,
    Param,
    RefInfo,
    Fig,
    Section,
    SeeAlsoItem,
    Signature,
    encoder,
    register,
    FullQual,
    Cannonical,
)
from .tree import PostDVR, resolve_, TreeVisitor
from .utils import progress, dummy_progress


warnings.simplefilter("ignore", UserWarning)


FORMAT = "%(message)s"
logging.basicConfig(
    level="INFO", format=FORMAT, datefmt="[%X]", handlers=[RichHandler()]
)

log = logging.getLogger("papyri")


def find_all_refs(
    graph_store: GraphStore,
) -> Tuple[FrozenSet[RefInfo], Dict[str, RefInfo]]:
    assert isinstance(graph_store, GraphStore)
    o_family = sorted(list(graph_store.glob((None, None, "module", None))))

    # TODO
    # here we can't compute just the dictionary and use frozenset(....values())
    # as we may have multiple version of lisbraries; this is something that will
    # need to be fixed in the long run
    known_refs = []
    ref_map = {}
    for item in o_family:
        r = RefInfo(item.module, item.version, "module", item.path)
        known_refs.append(r)
        ref_map[r.path] = r
    return frozenset(known_refs), ref_map


@register(4010)
@dataclass
class IngestedBlobs(Node):

    __slots__ = (
        "_content",
        "refs",
        "ordered_sections",
        "item_file",
        "item_line",
        "item_type",
        "aliases",
        "example_section_data",
        "see_also",
        "signature",
        "references",
        "logo",
        "qa",
        "arbitrary",
    )

    _content: Dict[str, Section]
    ordered_sections: List[str]
    item_file: Optional[str]
    item_line: Optional[int]
    item_type: Optional[str]
    aliases: List[str]
    example_section_data: Section
    see_also: List[SeeAlsoItem]  # see also data
    signature: Signature
    references: Optional[List[str]]
    qa: str
    arbitrary: List[Section]

    __isfrozen = False

    def __init__(self, *args, **kwargs):
        super().__init__()
        self._content = kwargs.pop("_content", None)
        self.example_section_data = kwargs.pop("example_section_data", None)
        self.refs = kwargs.pop("refs", None)
        self.ordered_sections = kwargs.pop("ordered_sections", None)
        self.item_file = kwargs.pop("item_file", None)
        self.item_line = kwargs.pop("item_line", None)
        self.item_type = kwargs.pop("item_type", None)
        self.aliases = kwargs.pop("aliases", [])
        self.see_also = kwargs.pop("see_also", None)
        assert "version" not in kwargs
        self.signature = kwargs.pop("signature", None)
        self.references = kwargs.pop("references", None)
        assert "logo" not in kwargs
        self.qa = kwargs.pop("qa", None)
        self.arbitrary = kwargs.pop("arbitrary", None)
        if self.arbitrary:
            for a in self.arbitrary:
                assert isinstance(a, Section), a
        assert not kwargs, kwargs
        assert not args, args
        self._freeze()

    def __setattr__(self, key, value):
        if self.__isfrozen and not hasattr(self, key):
            raise TypeError("%r is a frozen class" % self)
        object.__setattr__(self, key, value)

    def _freeze(self):
        self.__isfrozen = True

    @property
    def content(self):
        """
        List of sections in the doc blob docstrings

        """
        return self._content

    @content.setter
    def content(self, new):
        assert False

    def process(
        self, known_refs, aliases: Optional[Dict[str, str]], verbose=True, *, version
    ) -> None:
        """
        Process a doc blob, to find all local and nonlocal references.
        """
        assert isinstance(known_refs, frozenset)
        assert self._content is not None
        _local_refs: List[List[str]] = []
        sections_ = [
            "Parameters",
            "Returns",
            "Raises",
            "Yields",
            "Attributes",
            "Other Parameters",
            "Warns",
            ##
            "Warnings",
            "Methods",
            # "Summary",
            "Receives",
            # "Notes",
            # "Signature",
            #'Extended Summary',
            #'References'
            #'See Also'
            #'Examples'
        ]
        assert self.refs is not None
        assert aliases is not None

        for r in self.refs:
            assert None not in r
            aliases = {}
        for s in sections_:

            _local_refs = _local_refs + [
                [u.strip() for u in x[0].split(",")]
                for x in self.content.get(s, [])
                if isinstance(x, Param)
            ]

        def flat(l):
            return [y for x in l for y in x]

        local_refs = frozenset(flat(_local_refs))

        visitor = PostDVR(self.qa, known_refs, local_refs, aliases, version=version)
        for section in ["Extended Summary", "Summary", "Notes"] + sections_:
            if section not in self.content:
                continue
            assert section in self.content
            self.content[section] = visitor.visit(self.content[section])
        if (len(visitor.local) or len(visitor.total)) and verbose:
            # TODO: reenable assert len(visitor.local) == 0, f"{visitor.local} | {self.qa}"
            log.info("Newly found %s links in %s", len(visitor.total), repr(self.qa))
            for a, b in visitor.total:
                log.info("     %s refers to %s", repr(a), repr(b))

        self.example_section_data = visitor.visit(self.example_section_data)

        self.arbitrary = [visitor.visit(s) for s in self.arbitrary]

        for d in self.see_also:
            new_desc = []
            for dsc in d.descriptions:
                new_desc.append(visitor.visit(dsc))
            d.descriptions = new_desc
        try:
            for r in visitor._targets:
                assert None not in r, r
            self.refs = list(set(visitor._targets).union(set(self.refs)))

            for r in self.refs:
                assert None not in r
        except Exception as e:
            raise type(e)(self.refs)


def load_one_uningested(
    bytes_: bytes, bytes2_: Optional[bytes], qa, known_refs, aliases, *, version
) -> IngestedBlobs:
    """
    Load the json from a DocBlob and make it an ingested blob.
    """
    data = json.loads(bytes_)

    old_data = DocBlob.from_json(data)
    assert hasattr(old_data, "arbitrary")

    blob = IngestedBlobs()
    blob.qa = qa
    # TODO: here or maybe somewhere else:
    # see also 3rd item description is improperly deserialised as now it can be a paragraph.
    # Make see Also an auto deserialised object in take2.
    blob.see_also = old_data.see_also

    for k in old_data.slots():
        setattr(blob, k, getattr(old_data, k))

    blob.refs = data.pop("refs", [])
    assert bytes2_ is None

    blob.see_also = list(sorted(set(blob.see_also), key=lambda x: x.name.value))
    blob.example_section_data = blob.example_section_data
    blob.refs = []

    sections_ = [
        "Parameters",
        "Returns",
        "Raises",
        "Yields",
        "Attributes",
        "Other Parameters",
        "Warns",
        ##"Warnings",
        "Methods",
        # "Summary",
        "Receives",
    ]

    _local_refs: List[List[str]] = []

    for s in sections_:

        _local_refs = _local_refs + [
            [u.strip() for u in x[0].split(",")]
            for x in blob.content.get(s, [])
            if isinstance(x, Param)
        ]

    def flat(l) -> List[str]:
        return [y for x in l for y in x]

    local_refs: FrozenSet[str] = frozenset(flat(_local_refs))

    visitor = PostDVR(qa, frozenset(), local_refs, aliases=aliases, version=version)
    for section in ["Extended Summary", "Summary", "Notes"] + sections_:
        if section in blob.content:
            blob.content[section] = visitor.visit(blob.content[section])

    acc1 = []
    for sec in blob.arbitrary:
        acc1.append(visitor.visit(sec))
    blob.arbitrary = acc1

    blob.process(known_refs=known_refs, aliases=aliases, verbose=False, version=version)

    return blob


class Ingester:
    def __init__(self, dp):
        self.ingest_dir = ingest_dir
        self.gstore = GraphStore(self.ingest_dir)
        self.progress = dummy_progress if dp else progress

    def _ingest_narrative(self, path, gstore: GraphStore) -> None:
        meta = json.loads((path / "papyri.json").read_text())
        version = meta["version"]
        for _console, document in self.progress(
            (path / "docs").glob("*"), description=f"{path.name} Reading narrative docs"
        ):

            doc = load_one_uningested(
                document.read_text(),
                None,
                qa=document.name,
                known_refs=frozenset(),
                aliases={},
                version=None,
            )
            ref = document.name

            module, version = path.name.split("_")
            key = Key(module, version, "docs", ref)
            doc.validate()
            assert not doc.refs, doc.refs
            gstore.put(
                key,
                encoder.encode(doc),
                [],
            )

    def _ingest_examples(
        self, path: Path, gstore: GraphStore, known_refs, aliases, version, root
    ):

        for _, fe in self.progress(
            (path / "examples/").glob("*"), description=f"{path.name} Reading Examples"
        ):
            s = Section.from_json(json.loads(fe.read_bytes()))
            visitor = PostDVR(
                f"TBD (examples, {path}), supposed to be QA",
                known_refs,
                set(),
                aliases,
                version=version,
            )
            s_code = visitor.visit(s)
            refs = list(map(lambda s: Key(*s), visitor._targets))
            try:
                gstore.put(
                    Key(root, version, "examples", fe.name),
                    encoder.encode(s_code),
                    refs,
                )
            except Exception:
                breakpoint()

    def _ingest_assets(self, path, root, version, aliases, gstore):
        for _, f2 in self.progress(
            (path / "assets").glob("*"),
            description=f"{path.name} Reading image files ...",
        ):
            gstore.put(Key(root, version, "assets", f2.name), f2.read_bytes(), [])

        gstore.put(
            Key(root, version, "meta", "papyri.json"),
            cbor2.dumps(aliases),
            # json.dumps(aliases, indent=2).encode(),
            [],
        )

    def ingest(self, path: Path, check: bool) -> None:

        gstore = self.gstore

        known_refs, _ = find_all_refs(gstore)

        nvisited_items = {}

        ###

        meta_path = path / "papyri.json"
        data = json.loads(meta_path.read_text())
        version = data["version"]
        root = data["module"]
        # long : short
        aliases: Dict[str, str] = data.get("aliases", {})
        rev_aliases = {Cannonical(v): FullQual(k) for k, v in aliases.items()}
        meta = {k: v for k, v in data.items() if k != "aliases"}

        gstore.put_meta(root, version, encoder.encode(meta))

        self._ingest_examples(path, gstore, known_refs, aliases, version, root)
        self._ingest_assets(path, root, version, aliases, gstore)
        self._ingest_narrative(path, gstore)

        for _, f1 in self.progress(
            (path / "module").glob("*"),
            description=f"{path.name} Reading doc bundle files ...",
        ):
            assert f1.name.endswith(".json")
            qa = f1.name[:-5]
            if check:
                rqa = normalise_ref(qa)
                if rqa != qa:
                    # numpy weird thing
                    print(f"skip {qa=}, {rqa=}")
                    continue
                assert rqa == qa, f"{rqa} !+ {qa}"
            try:
                # TODO: version issue
                nvisited_items[qa] = load_one_uningested(
                    f1.read_text(),
                    None,
                    qa=qa,
                    known_refs=known_refs,
                    aliases=aliases,
                    version=version,
                )
                assert hasattr(nvisited_items[qa], "arbitrary")
            except Exception as e:
                raise RuntimeError(f"error Reading to {f1}") from e

        known_refs_II = frozenset(nvisited_items.keys())

        # TODO :in progress, crosslink needs version information.
        known_ref_info = frozenset(
            RefInfo(root, version, "module", qa) for qa in known_refs_II
        ).union(known_refs)

        for _, (qa, doc_blob) in self.progress(
            nvisited_items.items(), description=f"{path.name} Cross referencing"
        ):
            # todo: warning mutation.
            for sa in doc_blob.see_also:
                r = resolve_(
                    qa,
                    known_ref_info,
                    frozenset(),
                    sa.name.value,
                    rev_aliases=rev_aliases,
                )
                resolved, exists = r.path, r.kind
                if exists == "module":
                    sa.name.exists = True
                    if sa.name.reference != r:
                        log.warning(
                            f"Warning mutation on ingest from \n{sa.name.reference} to \n{r} in {qa}"
                        )
                    sa.name.ref = resolved

        for _, (qa, doc_blob) in self.progress(
            nvisited_items.items(), description=f"{path.name} Validating..."
        ):
            for k, v in doc_blob.content.items():
                assert isinstance(v, Section), f"section {k} is not a Section: {v!r}"
            try:
                doc_blob.validate()
            except Exception as e:
                raise type(e)(f"from {qa}")
            mod_root = qa.split(".")[0]
            assert mod_root == root, f"{mod_root}, {root}"
        for _, (qa, doc_blob) in self.progress(
            nvisited_items.items(), description=f"{path.name} Writing..."
        ):
            # for qa, doc_blob in nvisited_items.items():
            # we might update other modules with backrefs
            assert hasattr(doc_blob, "arbitrary")

            # TODO: FIX
            # when walking the tree of figure we can't properly crosslink
            # as we don't know the version number.
            # fix it at serialisation time.
            forward_refs = []
            for rq in doc_blob.refs:
                assert rq.version != "??"
                assert None not in rq
                forward_refs.append(Key(*rq))
            doc_blob.refs = []

            try:
                key = Key(mod_root, version, "module", qa)
                assert mod_root is not None
                assert version is not None
                assert None not in key
                assert not doc_blob.refs, doc_blob.refs
                gstore.put(
                    key,
                    encoder.encode(doc_blob),
                    forward_refs,
                )

            except Exception as e:
                raise RuntimeError(f"error writing to {path}") from e

    def relink(self) -> None:

        gstore = self.gstore
        known_refs, _ = find_all_refs(gstore)
        aliases: Dict[str, str] = {}
        for key in gstore.glob((None, None, "meta", "papyri.json")):
            aliases.update(cbor2.loads(gstore.get(key)))

        rev_aliases = {Cannonical(v): FullQual(k) for k, v in aliases.items()}

        builtins.print(
            "Relinking is safe to cancel, but some back references may be broken...."
        )
        builtins.print("Press Ctrl-C to abort...")

        visitor = TreeVisitor({RefInfo, Fig})
        for _, key in self.progress(
            gstore.glob((None, None, "module", None)), description="Relinking..."
        ):
            try:
                data, back, forward = gstore.get_all(key)
            except Exception as e:
                raise ValueError(str(key)) from e
            try:
                doc_blob = encoder.decode(data)
                assert isinstance(doc_blob, IngestedBlobs)
                # if res:
                # print("Refinfos...", len(res))
            except Exception as e:
                raise type(e)(key)
            assert doc_blob.content is not None, data

            # TODO: Move this into process ?
            res: Dict[Any, List[Any]] = {}
            for sec in (
                list(doc_blob.content.values())
                + [doc_blob.example_section_data]
                + doc_blob.arbitrary
                + doc_blob.see_also
            ):
                for k, v in visitor.generic_visit(sec).items():
                    res.setdefault(k, []).extend(v)

            assets_II = {Key(*f.value) for f in res.get(Fig, [])}
            ssr = set(
                [Key(*r) for r in res.get(RefInfo, []) if r.kind != "local"]
            ).union(assets_II)

            for sa in doc_blob.see_also:
                if sa.name.exists:
                    continue
                r = resolve_(
                    key.path,
                    known_refs,
                    frozenset(),
                    sa.name.value,
                    rev_aliases=rev_aliases,
                )
                if r.kind == "module":
                    print("unresolved ok...", r, key)
                    sa.name.exists = True
                    sa.name.reference = r

            # end todo

            data = encoder.encode(doc_blob)
            for s in forward:
                assert isinstance(s, Key)
            forward_refs = set(forward)
            if ssr != forward_refs:
                gstore.put(key, data, forward_refs)

        for _, key in progress(
            gstore.glob((None, None, "examples", None)),
            description="Relinking Examples...",
        ):
            s = encoder.decode(gstore.get(key))
            assert isinstance(s, Section), (s, key)
            dvr = PostDVR(
                f"TBD, supposed to be QA relink {key}",
                known_refs,
                set(),
                aliases,
                version="?",
            )
            s_code = dvr.visit(s)
            refs = [Key(*x) for x in dvr._targets]
            gstore.put(
                key,
                encoder.encode(s_code),
                refs,
            )


def main(path, check, *, dummy_progress):
    """
    Parameters
    ----------
    dummy_progress : bool
        whether to use a dummy progress bar instead of the rich one.
        Usefull when dropping into PDB.
        To be implemented. See gen step.
    check : <Insert Type here>
        <Multiline Description Here>
    path : <Insert Type here>
        <Multiline Description Here>
    """
    builtins.print("Ingesting", path.name, "...")
    from time import perf_counter

    now = perf_counter()

    assert path.exists(), f"{path} does not exists"
    assert path.is_dir(), f"{path} is not a directory"
    Ingester(dp=dummy_progress).ingest(path, check)
    delta = perf_counter() - now

    builtins.print(f"{path.name} Ingesting done in {delta:0.2f}s")


def relink(dummy_progress):
    Ingester(dp=dummy_progress).relink()
