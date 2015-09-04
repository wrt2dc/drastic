from datetime import datetime

from cassandra.cqlengine import columns
from cassandra.cqlengine.models import Model

from indigo.models.resource import Resource
from indigo.util import (
    default_cdmi_id,
    merge,
    split
)
from indigo.models.errors import UniqueException


class Collection(Model):
    id = columns.Text(default=default_cdmi_id, index=True)
    container = columns.Text(primary_key=True, required=False)
    name = columns.Text(primary_key=True, required=True)
    metadata = columns.Map(columns.Text, columns.Text, index=True)
    create_ts = columns.DateTime()
    modified_ts = columns.DateTime()
    is_root = columns.Boolean(default=False, index=True)

    # The access columns contain lists of group IDs that are allowed
    # the specified permission. If the lists have at least one entry
    # then access is restricted, if there are no entries in a particular
    # list, then access is granted to all (authenticated users)
    read_access = columns.List(columns.Text)
    edit_access = columns.List(columns.Text)
    write_access = columns.List(columns.Text)
    delete_access = columns.List(columns.Text)

    @classmethod
    def create(self, **kwargs):
        """We intercept the create call"""

        # TODO: Handle unicode chars in the name
        kwargs['name'] = kwargs['name'].strip()
#         if kwargs.has_key('path'):
#             kwargs['path'] = kwargs['path'].strip()
#         else:
#             kwargs['path'] = "{}{}/".format(kwargs['parent'],
#                                             kwargs['name'])
        d = datetime.now()
        kwargs['create_ts'] = d
        kwargs['modified_ts'] = d

        # TODO: raise Exception if path already exists
#         if not kwargs.get('parent'):
#             kwargs['path'] = u'/'
#         else:
#             parent = Collection.find_by_name(kwargs['parent'])
# 
#             # Make sure parent/name are not in use.
#             existing = self.objects.filter(parent=parent.id).all()
#             if kwargs['name'] in [e['name'] for e in existing]:
#                 raise UniqueException("That name is in use in the current collection")

        return super(Collection, self).create(**kwargs)

    @classmethod
    def create_root(cls):
        d = datetime.now()
        root = Collection(container='null',
                          name='Home',
                          is_root=True,
                          create_ts=d,
                          modified_ts=d)
        root.save()
        return root


    @classmethod
    def delete_all(self, path):
        parent_coll = Collection.find_by_path(path)
        colls = list(parent_coll.get_child_collections())
        rescs = list(parent_coll.get_child_resources())
        
        for resc in rescs:
            resc.delete()
        for coll in colls:
            Collection.delete_all(coll.path())
        parent_coll.delete()

    def update(self, **kwargs):
        kwargs['modified_ts'] = datetime.now()
        return super(Collection, self).update(**kwargs)

    def get_child_collections(self):
        return Collection.objects.filter(container=self.path()).all()

    def get_child_collection_count(self):
        return Collection.objects.filter(container=self.path()).count()

    def get_child_resources(self):
        return Resource.objects.filter(container=self.path()).all()

    def get_child_resource_count(self):
        return Resource.objects.filter(container=self.path()).count()

    def get_parent_collection(self):
        return Collection.find_by_path(self.container)

    @classmethod
    def get_root_collection(self):
        return self.objects.filter(is_root=True).first()

    @classmethod
    def find(self, path):
        return self.find_by_path(path)

    @classmethod
    def find_by_path(cls, path):
        if path == '/':
            return cls.get_root_collection()
        container, name = split(path)
        return cls.objects.filter(container=container, name=name).first()

    @classmethod
    def find_by_name(self, name):
        return self.objects.filter(name=name).first()

    @classmethod
    def find_by_id(self, idstring):
        return self.objects.filter(id=idstring).first()

    def __unicode__(self):
        return self.path()

    def user_can(self, user, action):
        """
        User can perform the action if any of the user's group IDs
        appear in this list for 'action'_access in this object.
        """
        if user.administrator:
            return True
        l = getattr(self, '{}_access'.format(action))
        if len(l) and not len(user.groups):
            # Group access required, user not in any groups
            return False
        if not len(l):
            # Group access not required
            return True

        # if groups has less than user.groups then it has had a group
        # removed, it confirms presence in l
        groups = set(user.groups) - set(l)
        return len(groups) < len(user.groups)

    def to_dict(self,user=None):
        data = {
            "id": self.id,
            "container": self.container,
            "name": self.name,
            "path": self.path(),
            "created": self.create_ts,
            "metadata": [(k,v) for k,v in self.metadata.iteritems()]
        }
        if user:
            data['can_read'] = self.user_can(user, "read")
            data['can_write'] = self.user_can(user, "write")
            data['can_edit'] = self.user_can(user, "edit")
            data['can_delete'] = self.user_can(user, "delete")

        return data

    def path(self):
        if self.is_root:
            return u"/"
        else:
            return merge(self.container, self.name)