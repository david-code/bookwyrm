""" using a bookwyrm instance as a source of book data """
from dataclasses import asdict, dataclass
from functools import reduce
import operator

from django.contrib.postgres.search import SearchRank, SearchQuery
from django.db.models import F, Q, Subquery, Window
from django.db.models.functions import Rank

from bookwyrm import models
from bookwyrm import connectors
from bookwyrm.settings import MEDIA_FULL_URL


# pylint: disable=arguments-differ
def search(query, min_confidence=0, filters=None, return_first=False):
    """search your local database"""
    filters = filters or []
    return _generic_search(query, min_confidence, filters, return_first, dedup=True)


def search_user_shelves(
    query, user, min_confidence=0, filters=None, return_first=False, start=None
):
    filters = (filters or []) + [Q(shelfbook__user=user)]
    return _generic_search(
        query, min_confidence, filters, return_first, dedup=False, start=start
    )


def _generic_search(
    query, min_confidence=0, filters=None, return_first=False, dedup=True, start=None
):
    if not query:
        return []
    query = query.strip()

    results = None
    # first, try searching unique identifiers
    # unique identifiers never have spaces, title/author usually do
    if not " " in query:
        results = search_identifiers(
            query, *filters, return_first=return_first, start=start
        )

    # if there were no identifier results...
    if not results:
        # then try searching title/author
        results = search_title_author(
            query, min_confidence, *filters, return_first=return_first, start=start
        )
    return results


def isbn_search(query):
    """search your local database"""
    if not query:
        return []
    # Up-case the ISBN string to ensure any 'X' check-digit is correct
    # If the ISBN has only 9 characters, prepend missing zero
    query = query.strip().upper().rjust(10, "0")
    filters = [{f: query} for f in ["isbn_10", "isbn_13"]]
    return models.Edition.objects.filter(
        reduce(operator.or_, (Q(**f) for f in filters))
    ).distinct()


def format_search_result(search_result):
    """convert a book object into a search result object"""
    cover = None
    if search_result.cover:
        cover = f"{MEDIA_FULL_URL}{search_result.cover}"

    return SearchResult(
        title=search_result.title,
        key=search_result.remote_id,
        author=search_result.author_text,
        year=search_result.published_date.year
        if search_result.published_date
        else None,
        cover=cover,
        confidence=search_result.rank if hasattr(search_result, "rank") else 1,
        connector="",
    ).json()


def search_identifiers(query, *filters, return_first=False, start=None):
    """tries remote_id, isbn; defined as dedupe fields on the model"""
    if connectors.maybe_isbn(query):
        # Oh did you think the 'S' in ISBN stood for 'standard'?
        normalized_isbn = query.strip().upper().rjust(10, "0")
        query = normalized_isbn
    # pylint: disable=W0212
    or_filters = [
        {f.name: query}
        for f in models.Edition._meta.get_fields()
        if hasattr(f, "deduplication_field") and f.deduplication_field
    ]
    objects = start or models.Edition.objects
    results = objects.filter(
        *filters, reduce(operator.or_, (Q(**f) for f in or_filters))
    ).distinct()

    if return_first:
        return results.first()
    return results


def search_title_author(
    query, min_confidence, *filters, return_first=False, dedup=True, start=None
):

    """searches for title and author"""
    query = SearchQuery(query, config="simple") | SearchQuery(query, config="english")
    objects = start or models.Edition.objects

    results = (
        objects.filter(*filters, search_vector=query)
        .annotate(rank=SearchRank(F("search_vector"), query))
        .filter(rank__gt=min_confidence)
        .order_by("-rank")
    )

    if not dedup:
        return results

    subquery = models.Edition.objects.annotate(
        search_rank=Subquery(results.values("rank")),
    ).annotate(
        rank=Window(
            expression=Rank(),
            partition_by=[F("parent_work__id")],
            order_by=F("search_rank").asc(),
        )
    )

    books = models.Edition.objects.annotate(
        rank=Subquery(subquery.values("rank"))
    ).filter(rank=1)

    if return_first:
        return books.first()
    return books


@dataclass
class SearchResult:
    """standardized search result object"""

    title: str
    key: str
    connector: object
    view_link: str = None
    author: str = None
    year: str = None
    cover: str = None
    confidence: int = 1

    def __repr__(self):
        # pylint: disable=consider-using-f-string
        return "<SearchResult key={!r} title={!r} author={!r} confidence={!r}>".format(
            self.key, self.title, self.author, self.confidence
        )

    def json(self):
        """serialize a connector for json response"""
        serialized = asdict(self)
        del serialized["connector"]
        return serialized
