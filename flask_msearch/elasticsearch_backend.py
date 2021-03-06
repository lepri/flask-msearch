#!/usr/bin/env python
# -*- coding: utf-8 -*-
# **************************************************************************
# Copyright © 2017-2020 jianglin
# File Name: elasticsearch_backend.py
# Author: jianglin
# Email: mail@honmaple.com
# Created: 2017-09-20 15:13:22 (CST)
# Last Update: Monday 2020-05-18 22:53:15 (CST)
#          By:
# Description:
# **************************************************************************
from sqlalchemy import types
from elasticsearch import Elasticsearch
from .backends import BaseBackend, BaseSchema, relation_column
import sqlalchemy

es = Elasticsearch()
try:
    es_version = es.info().get('version').get('number')
except AttributeError:
    es_version = '6'


class Schema(BaseSchema):

    def _fields(self):
        # check if we have a geohash field
        model = self.index.model
        schema_fields = {}
        if hasattr(model, '__msearch__geo__'):
            geo = getattr(model, '__msearch__geo__')
            for item in geo:
                schema_fields[item] = {'type': 'geo_point'}
        return schema_fields

    def fields_map(self, field_type):
        if field_type == "primary":
            return {'type': 'keyword'}

        type_map = {
            'date': types.Date,
            'datetime': types.DateTime,
            'boolean': types.Boolean,
            'integer': types.Integer,
            'float': types.Float,
            'binary': types.Binary
        }

        if isinstance(field_type, str):
            field_type = type_map.get(field_type, types.Text)

        if not isinstance(field_type, type):
            field_type = field_type.__class__

        if issubclass(field_type, (types.DateTime, types.Date)):
            return {'type': 'date'}
        elif issubclass(field_type, types.Integer):
            return {'type': 'integer'}
        elif issubclass(field_type, types.Float):
            return {'type': 'float'}
        elif issubclass(field_type, types.Boolean):
            return {'type': 'boolean'}
        elif issubclass(field_type, types.Binary):
            return {'type': 'binary'}
        return {'type': 'text'}


# https://medium.com/@federicopanini/elasticsearch-6-0-removal-of-mapping-types-526a67ff772
class Index(object):
    def __init__(self, client, model, doc_type, pk, name):
        '''
        global index name do nothing, must create different index name
        '''
        self._client = client
        self.model = model
        self.doc_type = getattr(
            model,
            "__msearch_index__",
            doc_type,
        )
        self.pk = getattr(
            model,
            "__msearch_primary_key__",
            pk,
        )
        self.searchable = set(
            getattr(
                model,
                "__msearch__",
                getattr(model, "__searchable__", []),
            ))
        self.geo = set(
            getattr(
                model,
                "__msearch__",
                getattr(model, "__msearch__geo__", []),
            ))
        self.name = self.doc_type
        self.init()

    def init(self):
        if not self._client.indices.exists(index=self.name):
            schema = Schema(self)
            mappings = {"mappings": {"properties": schema.fields}}
            self._client.indices.create(index=self.name, body=mappings)

    def create(self, **kwargs):
        """Create document not create index."""
        if es_version >= '6':
            kw = dict(index=self.name)
        else:
            kw = dict(index=self.name, doc_type=self.doc_type)
        kw.update(**kwargs)
        return self._client.index(**kw)

    def update(self, **kwargs):
        """Update document not update index."""
        if es_version >= '6':
            kw = dict(index=self.name, ignore=[404])
        else:
            kw = dict(index=self.name, doc_type=self.doc_type, ignore=[404])
        kw.update(**kwargs)
        return self._client.update(**kw)

    def delete(self, **kwargs):
        """Delete document not delete index."""
        if es_version >= '6':
            kw = dict(index=self.name, ignore=[404])
        else:
            kw = dict(index=self.name, doc_type=self.doc_type, ignore=[404])
        kw.update(**kwargs)
        return self._client.delete(**kw)

    def search(self, **kwargs):
        if es_version >= '6':
            kw = dict(index=self.name)
        else:
            kw = dict(index=self.name, doc_type=self.doc_type)
        kw.update(**kwargs)
        return self._client.search(**kw)

    def commit(self):
        return self._client.indices.refresh(index=self.name)


class ElasticSearch(BaseBackend):
    def init_app(self, app):
        self._setdefault(app)
        self._signal_connect(app)
        self._client = Elasticsearch(**app.config.get('ELASTICSEARCH', {}))
        self.pk = app.config["MSEARCH_PRIMARY_KEY"]
        self.index_name = app.config["MSEARCH_INDEX_NAME"]
        super(ElasticSearch, self).init_app(app)

    @property
    def indices(self):
        return self._client.indices

    def create_one_index(self,
                         instance,
                         update=False,
                         delete=False,
                         commit=True):
        if update and delete:
            raise ValueError("update and delete can't work together")
        ix = self.index(instance.__class__)
        pk = ix.pk
        pkv = getattr(instance, pk)
        attrs = dict()
        for field in ix.searchable:
            if '.' in field:
                attrs[field] = relation_column(instance, field.split('.'))
            else:
                attrs[field] = getattr(instance, field)
        if delete:
            self.logger.debug('deleting index: {}'.format(instance))
            r = ix.delete(**{pk: pkv})
        elif update:
            self.logger.debug('updating index: {}'.format(instance))
            r = ix.update(**{pk: pkv, "body": {"doc": attrs}})
        else:
            self.logger.debug('creating index: {}'.format(instance))
            r = ix.create(**{pk: pkv, "body": attrs})
        if commit:
            ix.commit()
        return r

    def index(self, model):
        """
        Elasticsearch multi types has been removed
        Use multi index unless set __msearch_index__.
        """
        name = model.__table__.name

        if name not in self._indexs:
            self._indexs[name] = Index(
                self._client,
                model,
                name,
                self.pk,
                self.index_name,
            )
        return self._indexs[name]

    def _fields(self, index, attr):
        return {index.pk: attr.pop(index.pk), 'body': {"doc": attr}}

    def msearch(self, m, query=None):
        return self.index(m).search(body=query)

    def _query_class(self, q):
        _self = self

        class Query(q):
            def msearch(self,
                        query,
                        fields=None,
                        limit=None,
                        or_=False,
                        rank_order=False,
                        geohash=None,
                        geofield=None,
                        geodistance='10km',
                        **kwargs):
                model = self._mapper_zero().class_
                # https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-query-string-query.html
                ix = _self.index(model)
                # we need to remove geofield from query if exists
                geo = ix.geo
                searchable = ix.searchable
                query_string = {
                    "fields": fields or [field for field in searchable if field not in geo],
                    "query": query,
                    "default_operator": "OR" if or_ else "AND",
                    "analyze_wildcard": True
                }
                query_string.update(**kwargs)
                if geohash and geofield and query:
                    query = {
                        "query": {
                            "bool": {
                                "must": {
                                    "query_string": query_string
                                },
                                "filter": {
                                    "geo_distance": {
                                        "distance": "50km",
                                        geofield: geohash
                                    },
                                },
                            },
                        },
                    }
                elif geohash and geofield and not query:
                    query = {
                        "query": {
                            "bool": {
                                "must": {
                                    "match_all": {}
                                },
                                "filter": {
                                    "geo_distance": {
                                        "distance": geodistance,
                                        geofield: geohash
                                    },
                                },
                            },
                        },
                    }
                else:
                    query = {
                        "query": {
                            "query_string": query_string
                        },
                        "size": limit or -1,
                    }
                results = _self.msearch(model, query)['hits']['hits']
                if not results:
                    return self.filter(sqlalchemy.text('null'))
                result_set = set()
                for i in results:
                    result_set.add(i["_id"])
                result_query = self.filter(getattr(model, ix.pk).in_(result_set))
                if rank_order:
                    result_query = result_query.order_by(
                        sqlalchemy.sql.expression.case(
                            {r["_id"]: index for index, r in enumerate(results)},
                            value=getattr(model, ix.pk)))
                return result_query

        return Query
