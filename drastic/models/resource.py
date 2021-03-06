"""Resource Model
"""
__copyright__ = "Copyright (C) 2016 University of Maryland"
__license__ = "GNU AFFERO GENERAL PUBLIC LICENSE, Version 3"


from datetime import datetime
import json
import logging
from cassandra.cqlengine import columns
from cassandra.cqlengine.models import Model
from paho.mqtt import publish

from drastic.models.errors import (
    NoSuchCollectionError,
    ResourceConflictError
)
from drastic.acl import serialize_acl_metadata
from drastic.util import (
    decode_meta,
    default_cdmi_id,
    meta_cassandra_to_cdmi,
    meta_cdmi_to_cassandra,
    merge,
    metadata_to_list,
    split,
    datetime_serializer
)


class Resource(Model):
    """Resource Model"""
    id = columns.Text(default=default_cdmi_id, index=True)
    container = columns.Text(primary_key=True, required=True)
    name = columns.Text(primary_key=True, required=True)
    checksum = columns.Text(required=False)
    size = columns.BigInt(required=False, default=0, index=True)
    metadata = columns.Map(columns.Text, columns.Text, index=True)
    mimetype = columns.Text(required=False)
    url = columns.Text(required=False)
    create_ts = columns.DateTime()
    modified_ts = columns.DateTime()
    file_name = columns.Text(required=False, default="")
    type = columns.Text(required=False, default='UNKNOWN')

    # The access columns contain lists of group IDs that are allowed
    # the specified permission. If the lists have at least one entry
    # then access is restricted, if there are no entries in a particular
    # list, then access is granted to all (authenticated users)
    read_access = columns.List(columns.Text)
    edit_access = columns.List(columns.Text)
    write_access = columns.List(columns.Text)
    delete_access = columns.List(columns.Text)

    logger = logging.getLogger('database')

    @classmethod
    def create(cls, **kwargs):
        """Create a new resource

        When we create a resource, the minimum we require is a name
        and a container. There is little chance of getting trustworthy
        versions of any of the other data at creation stage.
        """

        # TODO: Allow name starting or ending with a space ?
#         kwargs['name'] = kwargs['name'].strip()
        kwargs['create_ts'] = datetime.now()
        kwargs['modified_ts'] = kwargs['create_ts']

        if 'metadata' in kwargs:
            kwargs['metadata'] = meta_cdmi_to_cassandra(kwargs['metadata'])

        # Check the container exists
        from drastic.models.collection import Collection
        collection = Collection.find_by_path(kwargs['container'])

        if not collection:
            raise NoSuchCollectionError(kwargs['container'])

        # Make sure parent/name are not in use.
        existing = cls.objects.filter(container=kwargs['container']).all()
        if kwargs['name'] in [e['name'] for e in existing]:
            raise ResourceConflictError(merge(kwargs['container'],
                                              kwargs['name']))

        res = super(Resource, cls).create(**kwargs)

        res.mqtt_publish('create')

        return res

    def mqtt_publish(self, operation):
        payload = dict()
        payload['id'] = self.id
        payload['url'] = self.url
        payload['container'] = self.container
        payload['name'] = self.name
        payload['create_ts'] = self.create_ts
        payload['modified_ts'] = self.modified_ts
        payload['metadata'] = meta_cassandra_to_cdmi(self.metadata)
        topic = '{2}/resource{0}/{1}'.format(self.container, self.name, operation)
        # Clean up the topic by removing superfluous slashes.
        topic = '/'.join(filter(None, topic.split('/')))
        # Remove MQTT wildcards from the topic. Corner-case: If the resource name is made entirely of # and + and a
        # script is set to run on such a resource name. But that's what you get if you use stupid names for things.
        topic = topic.replace('#', '').replace('+', '')
        logging.info('Publishing on topic "{0}"'.format(topic))
        publish.single(topic, json.dumps(payload, default=datetime_serializer))

    def delete(self):
        self.mqtt_publish('delete')
        super(Resource, self).delete()

    @classmethod
    def find_by_id(cls, id_string):
        """Find resource by id"""
        return cls.objects.filter(id=id_string).first()

    @classmethod
    def find_by_path(cls, path):
        """Find resource by path"""
        coll_name, resc_name = split(path)
        return cls.objects.filter(container=coll_name, name=resc_name).first()

    def __unicode__(self):
        return self.path()

    def get_acl_metadata(self):
        """Return a dictionary of acl based on the Resource schema"""
        return serialize_acl_metadata(self)

    def get_container(self):
        """Returns the parent collection of the resource"""
        # Check the container exists
        from drastic.models.collection import Collection
        container = Collection.find_by_path(self.container)
        if not container:
            raise NoSuchCollectionError(self.container)
        else:
            return container

    def get_metadata(self):
        """Return a dictionary of metadata"""
        return meta_cassandra_to_cdmi(self.metadata)

    def get_metadata_key(self, key):
        """Return the value of a metadata"""
        return decode_meta(self.metadata.get(key, ""))

    def md_to_list(self):
        """Transform metadata to a list of couples for web ui"""
        return metadata_to_list(self.metadata)

    def path(self):
        """Return the full path of the resource"""
        return merge(self.container, self.name)

    def to_dict(self, user=None):
        """Return a dictionary which describes a resource for the web ui"""
        data = {
            "id": self.id,
            "name": self.name,
            "container": self.container,
            "path": self.path(),
            "checksum": self.checksum,
            "size": self.size,
            "metadata": self.md_to_list(),
            "create_ts": self.create_ts,
            "modified_ts": self.modified_ts,
            "mimetype": self.mimetype or "application/octet-stream",
            "type": self.type,
            "filename": self.file_name,
            "url": self.url,
        }
        if user:
            data['can_read'] = self.user_can(user, "read")
            data['can_write'] = self.user_can(user, "write")
            data['can_edit'] = self.user_can(user, "edit")
            data['can_delete'] = self.user_can(user, "delete")
        return data

    def update(self, **kwargs):
        """Update a resource"""
        kwargs['modified_ts'] = datetime.now()

        if 'metadata' in kwargs:
            kwargs['metadata'] = meta_cdmi_to_cassandra(kwargs['metadata'])

        super(Resource, self).update(**kwargs)

        self.mqtt_publish('update')

        return self

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
