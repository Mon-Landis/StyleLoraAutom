from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Protocol
from stylelora.project_db.errors import ValidationError
from stylelora.tagging.normalization import normalize_tag


class UnsupportedSorter(ValidationError):
    pass


@dataclass(frozen=True)
class SortContext:
    catalog_revision: str = ''
    skill_hash: str = 'r4-sort-v1'
    categories: Mapping[str,str] = field(default_factory=dict)


class TagSorter(Protocol):
    version: str
    def sort(self,tags: tuple[str,...],context: SortContext) -> tuple[str,...]: ...


class PyramidSorter:
    version = 'r4-sort-v1'
    order = {'subject':0,'character':0,'count':0,'action':1,'expression':1,'clothing':2,
             'composition':3,'view':3,'scene':4,'lighting':5}

    def __init__(self,context=SortContext()):
        self.context = context

    def sort(self,tags,context):
        return tuple(sorted(tags,key=lambda t:self.order.get(context.categories.get(t),6)))


def checked_sort(sorter,tags):
    if sorter is None:
        raise UnsupportedSorter('sorter:unsupported')
    result = sorter.sort(tags,getattr(sorter,'context',SortContext()))
    if not isinstance(result,tuple) or len(result)!=len(tags) or set(result)!=set(tags) or len(set(map(normalize_tag,result)))!=len(tags):
        raise ValidationError('sorter:contract_violation')
    return result
