from dataclasses import dataclass

type Hop = tuple[type, str]


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A field of `owner`, reached from the filtered class through the relations in `via`."""

    owner: type
    name: str
    via: tuple[Hop, ...]


class Path:
    """The object at the end of the relations in `via`; its attributes are that object's fields."""

    # Name-mangled so they never hide a field of the target class.
    __slots__ = ("__target", "__via")

    def __init__(self, target: type, via: tuple[Hop, ...]) -> None:
        object.__setattr__(self, "_Path__target", target)
        object.__setattr__(self, "_Path__via", via)

    def __getattr__(self, name: str) -> "FieldRef | Path":
        node = getattr(self.__target, name)
        if isinstance(node, FieldRef):
            return FieldRef(node.owner, node.name, self.__via + node.via)
        if isinstance(node, Path):
            return Path(node.__target, self.__via + node.__via)
        raise AttributeError(f"{self.__target.__name__} has no field {name!r}")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Path is immutable")
