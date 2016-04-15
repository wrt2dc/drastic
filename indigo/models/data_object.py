"""Resource Model

Copyright 2015 Archive Analytics Solutions

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from cStringIO import StringIO
import zipfile
from datetime import datetime
import json
import logging
from cassandra.cqlengine import (
    columns,
    connection
)
from cassandra.query import SimpleStatement
from cassandra.cqlengine.models import Model
from paho.mqtt import publish

from indigo import get_config
from indigo.models.errors import (
    NoSuchCollectionError,
    ResourceConflictError
)
from indigo.models import (
    Group,
    TreeEntry
)
from indigo.models.acl import (
    Ace,
    acemask_to_str,
    cdmi_str_to_aceflag,
    str_to_acemask,
    cdmi_str_to_acemask,
    serialize_acl_metadata
)
from indigo.util import (
    decode_meta,
    default_cdmi_id,
    meta_cassandra_to_cdmi,
    meta_cdmi_to_cassandra,
    merge,
    metadata_to_list,
    split,
    datetime_serializer
)
from indigo.models.search import SearchIndex



class DataObject(Model): 
    """ The DataObject represents actual data objects, the tree structure
    merely references it.

    Each partition key gathers together all the data under one partition (the
    CDMI ID ) and the object properties are represented using static columns
    (one instance per partition)
    It has a similar effect to a join to a properties table, except the
    properties are stored with the rest of the partition

    This is an 'efficient' model optimised for Cassandra's quirks.

    N.B. by default Cassandra compresses its data ( using LZW ), so we get that
    for free."""
    id = columns.Text(default=default_cdmi_id, required=True,
                      partition_key = True )    # The 'name' of the object
    #####################
    # These columns are the same (shared) between all entries with same id 
    # (they use the static attribute , [ like an inode or a header ] )
    #####################
    checksum        = columns.Text    (                       static = True )
    size            = columns.BigInt  ( default=0 ,           static = True )
    metadata        = columns.Map     ( columns.Text, columns.Text , static = True )
    mimetype        = columns.Text    (                       static = True )
    alt_url         = columns.Set     ( columns.Text        , static = True )
    create_ts       = columns.DateTime( default=datetime.now, static = True )
    modified_ts     = columns.DateTime(                       static = True )
    type            = columns.Text    ( required=False,static = True, default='UNKNOWN')
    acl             = columns.Map     ( columns.Text  , columns.UserDefinedType(Ace) , static = True )
    treepath        = columns.Text    ( static = True , required = False )   # A general aid to integrity ...
    #####################
    # And 'clever' bit -- 'here' data, These will be the only per-record-fields in the partition (i.e. object)
    # So the datastructure looks like a header , with an ordered list of blobs
    #####################
    sequence_number = columns.Integer(primary_key=True , partition_key = False )  # This is the 'clustering' key...
    blob = columns.Blob(required = False)
    compressed = columns.Boolean(default=False)
    #####################

    @classmethod
    def append_chunk(cls, id, data, sequence_number, compressed=False):
        data_object = cls(id=id,
                          sequence_number=sequence_number,
                          blob=data,
                          compressed=compressed)
        data_object.save()
        return data_object


    @classmethod
    def create(cls, data, compressed=False):
        """data: initial data"""
        new_id = default_cdmi_id()
        data_object = cls(id=new_id,
                          sequence_number=0,
                          blob=data,
                          compressed=compressed)
        data_object.save()
        return data_object


    def create_acl(self, read_access, write_access):
        #self.container_acl = {}
        #self.save()
        self.update_acl(read_access, write_access)


    @classmethod
    def delete_id(cls, uuid):
        cfg = get_config(None)
        session = connection.get_session()
        keyspace = cfg.get('KEYSPACE', 'indigo')
        session.set_keyspace(keyspace)
        query = SimpleStatement("""DELETE FROM data_object WHERE id=%s""")
        session.execute(query, (uuid,))


    @classmethod
    def find(cls, uuid):
        entries = cls.objects.filter(id=uuid)
        if not entries:
            return None
        else:
            return entries.first()


    def chunk_content(self):
        """
        Yields the content for the driver's URL, if any
        a chunk at a time.  The value yielded is the size of
        the chunk and the content chunk itself.
        """
        entries = DataObject.objects.filter(id=self.id)
        for entry in entries:
            if entry.compressed:
                data = StringIO(entry.blob)
                z = zipfile.ZipFile(data, 'r')
                content = z.read("data")
                data.close()
                z.close()
                yield content
            else:
                yield entry.blob


    def update_acl(self, read_access, write_access):
        """Replace the acl with the given list of access.
 
        read_access: a list of groups id that have read access for this
                     collection
        write_access: a list of groups id that have write access for this
                     collection
 
        """
        cfg = get_config(None)
        keyspace = cfg.get('KEYSPACE', 'indigo')
        # The ACL we construct will replace the existing one
        # The dictionary keys are the groups id for which we have an ACE
        # We don't use aceflags yet, everything will be inherited by lower
        # sub-collections
        # acemask is set with helper (read/write - see indigo/models/acl/py)
        access = {}
        for gid in read_access:
            access[gid] = "read"
        for gid in write_access:
            if gid in access:
                access[gid] = "read/write"
            else:
                access[gid] = "write"
         
        ls_access = []
        for gid in access:
            g = Group.find_by_id(gid)
            if g:
                ident = g.name
            elif gid.upper() == "AUTHENTICATED@":
                ident = "AUTHENTICATED@"
            else:
                # TODO log or return error if the identifier isn't found ?
                continue
            s = ("'{}': {{"
                 "acetype: 'ALLOW', "
                 "identifier: '{}', "
                 "aceflags: {}, "
                 "acemask: {}"
                 "}}").format(gid, ident, 0, str_to_acemask(access[gid], True))
            ls_access.append(s)
        acl = "{{{}}}".format(", ".join(ls_access))
        query= ("UPDATE {}.data_object SET acl = acl + {}"
                "WHERE id='{}'").format(
            keyspace,
            acl,
            self.id)
        connection.execute(query)


    def update_cdmi_acl(self, cdmi_acl):
        """Update acl with the metadata acl passed with a CDMI request"""
        cfg = get_config(None)
        session = connection.get_session()
        keyspace = cfg.get('KEYSPACE', 'indigo')
        session.set_keyspace(keyspace)
        ls_access = []
        for cdmi_ace in cdmi_acl:
            if 'identifier' in cdmi_ace:
                gid = cdmi_ace['identifier']
            else:
                # Wrong syntax for the ace
                continue
            g = Group.find(gid)
            if g:
                ident = g.name
            elif gid.upper() == "AUTHENTICATED@":
                ident = "AUTHENTICATED@"
            else:
                # TODO log or return error if the identifier isn't found ?
                continue
            s = ("'{}': {{"
                 "acetype: '{}', "
                 "identifier: '{}', "
                 "aceflags: {}, "
                 "acemask: {}"
                 "}}").format(g.id,
                              cdmi_ace['acetype'].upper(),
                              ident,
                              cdmi_str_to_aceflag(cdmi_ace['aceflags']),
                              cdmi_str_to_acemask(cdmi_ace['acemask'], False)
                             )
            ls_access.append(s)
        acl = "{{{}}}".format(", ".join(ls_access))
       
        query = """UPDATE data_object SET acl={} 
            WHERE id='{}'""".format(acl, self.id)
        session.execute(query)

