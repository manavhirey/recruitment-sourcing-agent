import json
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class IndustryCode:
    code: str
    label: str
    parent_code: str | None
    default_adjacency_codes: frozenset[str]


class IndustryTaxonomy:
    def __init__(self, version: str, industries: dict[str, IndustryCode]) -> None:
        self.version = version
        self._industries = industries

    @classmethod
    def load_version(cls, version: str) -> "IndustryTaxonomy":
        resource = files("app.clients").joinpath(f"industry_taxonomy.{version}.json")
        with resource.open() as taxonomy_file:
            document = json.load(taxonomy_file)
        if document["version"] != version:
            raise ValueError("industry_taxonomy_version_mismatch")
        industries = {
            node["code"]: IndustryCode(
                code=node["code"],
                label=node["label"],
                parent_code=node["parent_code"],
                default_adjacency_codes=frozenset(node["default_adjacency"]),
            )
            for node in document["industries"]
        }
        return cls(version, industries)

    def contains(self, code: str) -> bool:
        return code in self._industries

    def get(self, code: str) -> IndustryCode:
        return self._industries[code]

    def default_adjacency(self, code: str) -> set[str]:
        return set(self.get(code).default_adjacency_codes)

    def is_adjacent(self, source: str, target: str) -> bool:
        return target in self.default_adjacency(source)
