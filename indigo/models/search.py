"""
As Cassandra doesn't provide %LIKE% style queries we are constrained to
only having direct matches and manually checking across each specific
field.  This isn't ideal.

Until such a time as we have a better solution to searching, this model
provides a simple index (and very, very simple retrieval algorithm) for
matching words with resources and collections.  It does *not* search the
data itself.
"""

import uuid
from datetime import datetime

from cassandra.cqlengine import columns
from cassandra.cqlengine.models import Model

from indigo.models.errors import UniqueException
from indigo.util import default_id


class SearchIndex(Model):
    id        = columns.Text(primary_key=True, default=default_id)
    term        = columns.Text(required=True, index=True)
    object_type = columns.Text(required=True)
    object_id   = columns.Text(required=True, index=True)

    @classmethod
    def is_stop_word(cls, term):
        return term in ["a", "the", "of", "is"]

    @classmethod
    def find(cls, termstrings):
        # termstrings should have been lower cased and cleaned
        from indigo.models.collection import Collection

        def get_object(obj):
            if obj.object_type == 'Collection':
                result_obj = Collection.find_by_id(obj.object_id).to_dict()
                result_obj['type'] = 'Collection'
                return result_obj
            return None

        terms = [t for t in termstrings if not cls.is_stop_word(t)]

        result_objects = []
        for t in termstrings:
            if cls.is_stop_word(t):
                continue
            result_objects.extend(cls.objects.filter(term=t).all())

        results = []
        for result in result_objects:
            results.append(get_object(result))
        results = filter(lambda x: x, results)

        # Do some sane ordering here to group together by ID and
        # order by frequency. Add the hit_count to the object dictionary
        # and then we can order on that
        keys = set(r['id'] for r in results)

        result_list = []
        for k in keys:
            # get each element with this key, count them, store the hit
            # count and only add one to results
            matches = [x for x in results if x['id'] == k]
            match = matches[0]
            match['hit_count'] = len(matches)
            result_list.append(match)

        return sorted(result_list, key=lambda res: res.get('hit_count', 0), reverse=True)

    @classmethod
    def reset(cls, id):
        # Have to delete one at a time without a partition index.
        for obj in cls.objects.filter(object_id=id).all():
            obj.delete()

    @classmethod
    def index(cls, object, fields=['name']):
        result_count = 0

        def clean(t):
            return t.lower().replace('_', ' ').split(' ')

        terms = []
        for f in fields:
            terms.extend( clean(getattr(object, f)) )

        object_type = object.__class__.__name__

        for term in terms:
            if cls.is_stop_word(term):
                continue

            SearchIndex.create(term=term, object_type=object_type, object_id=object.id)
            result_count += 1

        return result_count


    def __unicode__(self):
        return unicode("".format(self.term, self.object_type ))

