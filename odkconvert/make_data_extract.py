#!/usr/bin/python3

# Copyright (c) 2022 Humanitarian OpenStreetMap Team
#
# This file is part of Odkconvert.
#
#     Odkconvert is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     Odkconvert is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with Odkconvert.  If not, see <https:#www.gnu.org/licenses/>.
#

import argparse
import os
import logging
import sys
import re
import yaml
import json
from geojson import Point, Polygon, Feature, FeatureCollection, dump
import geojson
from OSMPythonTools.overpass import Overpass
from odkconvert.filter_data import FilterData
from odkconvert.xlsforms import xlsforms_path
import requests
from requests.auth import HTTPBasicAuth


# from yamlfile import YamlFile
import psycopg2
from shapely.geometry import shape


# all possible queries
choices = [
    "buildings",
    "amenities",
    "toilets",
    "landuse",
    "emergency",
    "shops",
    "waste",
    "water",
    "education",
    "healthcare",
]

# Instantiate logger
log_level = os.getenv("LOG_LEVEL", default="INFO")
logging.getLogger("urllib3").setLevel(log_level)
log = logging.getLogger(__name__)


class DatabaseAccess(object):
    def __init__(self, dbhost=None, dbname=None):
        if dbname == "underpass":
            # Authentication data
            self.auth = HTTPBasicAuth(self.user, self.passwd)

            # Use a persistant connect, better for multiple requests
            self.session = requests.Session()
            self.url = "https://raw-data-api0.hotosm.org/"
        else:
            self.auth = None
            self.sesson = None
            self.url = None

    def createJson(self, category, boundary):
        path = xlsforms_path.replace("xlsforms", "data_models")
        file = open(f"{path}/{category}.yaml", "r").read()
        data = yaml.load(file, Loader=yaml.Loader)

        clip = open(boundary, "r")
        features = dict()
        geom = geojson.load(clip)
        features['geometry'] = geom['geometry']

        # The database tables to query
        # if tags exists, then only query those fields
        columns = dict()
        tags = data['where']['tags'][0]
        for tag, value in tags.items():
            if value == "not null":
                columns[tag] = []
        filters = {"tags": {"all_geometry": {"join_or": columns}}}
        features['filters'] = filters
        tables = list()
        for table in data['from']:
            if table == "nodes":
                tables.append("point")
            elif table == "ways_poly":
                tables.append("polygon")
            elif table == "ways_line":
                tables.append("linestring")
            elif table == "relations":
                pass
        features["geometryType"] = tables
        return json.dumps(features)

    def createSQL(self, category):
        path = xlsforms_path.replace("xlsforms", "data_models")
        file = open(f"{path}/{category}.yaml", "r").read()
        data = yaml.load(file, Loader=yaml.Loader)

        sql = list()
        # The database tables to query
        tables = data['from']
        for table in tables:
            query = "SELECT "
            select = data['select']
            # if tags exists, then only return those fields
            if 'tags' in select:
                for tag in select['tags']:
                    query += f" {select[tag]} AS {tag}, "
                query += "osm_id AS id"
            else:
                query += " * "
            query += f" FROM {table} "
            where = data['where']
            # if tags exists, then only query those fields
            if 'where' in data:
                query += " WHERE "
                tags = data['where']['tags'][0]
                for tag, value in tags.items():
                    if value == "not null":
                        query += f"tags->>\'{tag}\' IS NOT NULL OR "
            sql.append(query[:-4])
        return sql

class PostgresClient(DatabaseAccess):
    """Class to handle SQL queries for the categories"""

    def __init__(self, dbhost=None, dbname=None, output=None):
        """Initialize the postgres handler"""
        # OutputFile.__init__( self, output)
        self.boundary = None
        logging.info("Opening database connection to: %s" % dbhost)
        if dbhost == "localhost" and dbname != "underpass":
            connect = "PG: dbname=" + dbname
            # if dbhost:
            #     connect += " host=" + dbhost
            # self.pg = ogr.Open(connect)
            if dbhost is None or dbhost == "localhost":
                connect = f"dbname={dbname}"
            else:
                connect = f"host={dbhost} dbname={dbname}"
            try:
                self.dbshell = psycopg2.connect(connect)
                self.dbshell.autocommit = True
                self.dbcursor = self.dbshell.cursor()
                if self.dbcursor.closed == 0:
                    logging.info(f"Opened cursor in {dbname}")
            except Exception as e:
                logging.error("Couldn't connect to database: %r" % e)
        else:
            pass

    def getFeatures(self, boundary=None, filespec=None, category="buildings"):
        """Extract buildings from Postgres"""
        logging.info("Extracting features from Postgres...")

        config = self.createSQL(category)

        if type(boundary) != dict:
            clip = open(boundary, "r")
            geom = geojson.load(clip)
            poly = geom["geometry"]
        else:
            poly = boundary
        config = self.createJson(category, boundary)
        wkt = shape(poly)

        sql = f"DROP VIEW IF EXISTS ways_view;CREATE TEMP VIEW ways_view AS SELECT * FROM ways_poly WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{wkt.wkt}'), geom)"
        self.dbcursor.execute(sql)
        sql = f"DROP VIEW IF EXISTS nodes_view;CREATE TEMP VIEW nodes_view AS SELECT * FROM nodes WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{wkt.wkt}'), geom)"
        self.dbcursor.execute(sql)

        sql = f"DROP VIEW IF EXISTS relations_view;CREATE TEMP VIEW relations_view AS SELECT * FROM nodes WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{wkt.wkt}'), geom)"
        # self.dbcursor.execute(sql)

        features = list()
        for table in tables:
            logging.debug("Querying table %s with conditional %s" % (table, filter))
            query = f"SELECT {select} FROM {table} WHERE {filter}"
            self.dbcursor.execute(query)
            result = self.dbcursor.fetchall()
            logging.info("Query returned %d records" % len(result))
            for item in result:
                gps = item[0][16:-1].split(" ")
                poi = Point((float(gps[0]), float(gps[1])))
                tags = item[2]
                tags["id"] = item[1]
                if "name" in tags:
                    tags["title"] = tags["name"]
                    tags["label"] = tags["name"]
                else:
                    tags["title"] = None
                    tags["label"] = None
                features.append(Feature(geometry=poi, properties=tags))
        collection = FeatureCollection(features)

        cleaned = FilterData()
        models = xlsforms_path.replace("xlsforms", "data_models")
        cleaned.parse(f"{models}/Impact Areas - Data Models V1.1.xlsx")
        new = cleaned.cleanData(collection)
        json = open(filespec, "w")
        dump(new, json)

class OverpassClient(object):
    """Class to handle Overpass queries"""

    def __init__(self, output=None):
        """Initialize Overpass handler"""
        self.overpass = Overpass()
        OutputFile.__init__(self, output)

    def getFeatures(self, boundary=None, filespec=None, category="buildings"):
        """Extract buildings from Overpass"""
        logging.info("Extracting features...")
        poly = ogr.Open(boundary)
        layer = poly.GetLayer()

        filter = None
        if category == "buildings":
            filter = "building"
        elif category == "amenities":
            filter = "amenities"
        elif category == "landuse":
            filter = "landuse"
        elif category == "healthcare":
            filter = "healthcare='*'][social_facility='*'][healthcare:speciality='*'"
        elif category == "emergency":
            filter = "emergency"
        elif category == "education":
            filter = "amenity=school][amenity=kindergarden"
        elif category == "shops":
            filter = "shop"
        elif category == "waste":
            filter = '~amenity~"waste_*"'
        elif category == "water":
            filter = "amenity=water_point"
        elif category == "toilets":
            filter = "amenity=toilets"

        # Create a field in the output file for each tag from the yaml config file
        tags = self.getTags(category)
        if tags is None:
            logging.error("No data returned from Overpass!")
        if len(tags) > 0:
            for tag in tags:
                self.outlayer.CreateField(ogr.FieldDefn(tag, ogr.OFTString))
        self.fields = self.outlayer.GetLayerDefn()

        extent = layer.GetExtent()
        bbox = f"{extent[2]},{extent[0]},{extent[3]},{extent[1]}"
        query = f"(way[{filter}]({bbox}); node[{filter}]({bbox}); relation[{filter}]({bbox}); ); out body; >; out skel qt;"
        logging.debug(query)
        result = self.overpass.query(query)

        nodes = dict()
        if result.nodes() is None:
            logging.warning("No data found in this boundary!")
            return

        for node in result.nodes():
            wkt = "POINT(%f %f)" % (float(node.lon()), float(node.lat()))
            center = ogr.CreateGeometryFromWkt(wkt)
            nodes[node.id()] = center

        ways = result.ways()
        for way in ways:
            for ref in way.nodes():
                # FIXME: There's probably a better way to get the node ID.
                nd = ref._queryString.split("/")[1]
                feature = ogr.Feature(self.fields)
                feature.SetGeometry(nodes[float(nd)])
                # feature.SetField("id", way.id())
                for tag, val in way.tags().items():
                    if tag in tags:
                        feature.SetField(tag, val)
                self.addFeature(feature)
        self.outdata.Destroy()


class FileClient(object):
    """Class to handle Overpass queries"""

    def __init__(self, infile=None, output=None):
        """Initialize Overpass handler"""
        OutputFile.__init__(self, output)
        self.infile = infile

    def getFeatures(self, boundary=None, infile=None, outfile=None):
        """Extract buildings from a disk file"""
        logging.info("Extracting buildings from %s..." % infile)
        if boundary:
            poly = ogr.Open(boundary)
            layer = poly.GetLayer()
            ogr.Layer.Clip(osm, layer, memlayer)
        else:
            layer = poly.GetLayer()

        tmp = ogr.Open(infile)
        layer = tmp.GetLayer()

        layer.SetAttributeFilter("tags->>'building' IS NOT NULL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Make GeoJson data file for ODK from OSM"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    parser.add_argument(
        "-o", "--overpass", action="store_true", help="Use Overpass Turbo"
    )
    parser.add_argument(
        "-p", "--postgres", action="store_true", help="Use a postgres database"
    )
    parser.add_argument(
        "-g", "--geojson", default="tmp.geojson", help="Name of the GeoJson output file"
    )
    parser.add_argument("-i", "--infile", help="Input data file")
    parser.add_argument("-dn", "--dbname", help="Database name")
    parser.add_argument("-dh", "--dbhost", default="localhost", help="Database host")
    parser.add_argument(
        "-b", "--boundary", help="Boundary polygon to limit the data size"
    )
    parser.add_argument(
        "-c",
        "--category",
        default="buildings",
        choices=choices,
        help="Which category to extract",
    )
    args = parser.parse_args()

    # if verbose, dump to the terminal.
    if args.verbose is not None:
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        ch.setFormatter(formatter)
        root.addHandler(ch)

    if args.geojson == "tmp.geojson":
        # The data file name is in the XML file
        regex = r"jr://file.*\.geojson"
        outfile = None
        filename = args.category + ".xml"
        if not os.path.exists(filename):
            logging.error("Please run xls2xform or make to produce %s" % filename)
            quit()
        with open(filename, "r") as f:
            txt = f.read()
            match = re.search(regex, txt)
            if match:
                tmp = match.group().split("/")
        outfile = tmp[3]
    else:
        outfile = args.geojson

    if args.postgres:
        logging.info("Using a Postgres database for the data source")
        pg = PostgresClient(args.dbhost, args.dbname, outfile)
        pg.getFeatures(args.boundary, args.geojson, args.category)
        # pg.cleanup(outfile)
    elif args.overpass:
        logging.info("Using Overpass Turbo for the data source")
        op = OverpassClient(outfile)
        op.getFeatures(args.boundary, args.geojson, args.category)
    elif args.infile:
        f = FileClient(args.infile)
        f.getFeatures(args.boundary, args.geojson, args.category)
        logging.info("Using file %s for the data source" % args.infile)
    else:
        logging.error("You need to supply either --overpass or --postgres")

        logging.info("Wrote output data file to: %s" % outfile)
