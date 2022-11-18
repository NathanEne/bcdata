from datetime import datetime
from datetime import timedelta
import json
import logging
import math
from pathlib import Path
import os
from urllib.parse import urlencode
import sys
import warnings
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from owslib.wfs import WebFeatureService
import requests
import geopandas as gpd


import bcdata

if not sys.warnoptions:
    warnings.simplefilter("ignore")

log = logging.getLogger(__name__)

WFS_URL = "https://openmaps.gov.bc.ca/geo/pub/wfs"
OWS_URL = "http://openmaps.gov.bc.ca/geo/ows"

WFS = WebFeatureService(OWS_URL, version="2.0.0")


def get_sortkey(table, wfs_schema):
    """Check data for unique columns available for sorting paged requests"""
    columns = list(wfs_schema["properties"].keys())
    # use OBJECTID as default sort key, if present
    if "OBJECTID" in columns:
        return "OBJECTID"
    # if OBJECTID is not present (several GSR tables), use SEQUENCE_ID
    elif "SEQUENCE_ID" in columns:
        return "SEQUENCE_ID"
    # otherwise, it should be safe to presume first column is the primary key
    # (WHSE_FOREST_VEGETATION.VEG_COMP_LYR_R1_POLY's FEATURE_ID appears to be
    # the only public case, and very large veg downloads are likely better
    # accessed via some other channel)
    else:
        return columns[0]


def check_cache(path):
    """Return true if the cache file holding list of all datasets
    does not exist or is more than a day old
    (this is not very long, but checking daily seems to be a good strategy)
    """
    if not os.path.exists(path):
        return True
    else:
        # check the age
        mod_date = datetime.fromtimestamp(os.path.getmtime(path))
        if mod_date < (datetime.now() - timedelta(days=1)):
            return True
        else:
            return False


def validate_name(dataset):
    """Check wfs/cache and the bcdc api to see if dataset name is valid"""
    if dataset.upper() in list_tables():
        return dataset.upper()
    else:
        return bcdata.get_table_name(dataset.upper())


def list_tables(refresh=False, cache_file=None):
    """Return a list of all datasets available via WFS"""
    # default cache listing all objects available is
    # ~/.bcdata
    if not cache_file:
        if "BCDATA_CACHE" in os.environ:
            cache_file = os.environ["BCDATA_CACHE"]
        else:
            cache_file = os.path.join(str(Path.home()), ".bcdata")

    # regenerate the cache if:
    # - the cache file doesn't exist
    # - we force a refresh
    # - the cache is older than 1 day
    if refresh or check_cache(cache_file):
        bcdata_objects = [i.strip("pub:") for i in list(WFS.contents)]
        with open(cache_file, "w") as outfile:
            json.dump(sorted(bcdata_objects), outfile)
    else:
        with open(cache_file, "r") as infile:
            bcdata_objects = json.load(infile)

    return bcdata_objects


def get_count(dataset, query=None):
    """Ask DataBC WFS how many features there are in a table/query"""
    # https://gis.stackexchange.com/questions/45101/only-return-the-numberoffeatures-in-a-wfs-query
    table = validate_name(dataset)
    payload = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": table,
        "resultType": "hits",
        "outputFormat": "json",
    }
    if query:
        payload["CQL_FILTER"] = query
    try:
        r = requests.get(WFS_URL, params=payload)
        log.debug(r.url)
        r.raise_for_status()  # check status code is 200
    except requests.exceptions.HTTPError as err:  # fail if not 200
        raise SystemExit(err)
    return int(ET.fromstring(r.text).attrib["numberMatched"])


def make_request(url):
    """Submit a getfeature request to DataBC WFS and return features"""
    try:
        r = requests.get(url)
        log.info(r.url)
        log.debug(r.headers)
        r.raise_for_status()  # check status code is 200
    except requests.exceptions.HTTPError as err:  # fail if not 200
        print(log.debug(r.headers))
        raise SystemExit(err)

    return r.json()["features"]  # return features if status code is 200


def define_requests(
    dataset,
    query=None,
    crs="epsg:4326",
    bounds=None,
    bounds_crs="EPSG:3005",
    count=None,
    sortby=None,
    pagesize=10000,
):
    """Translate provided parameters into a list of WFS request URLs required
    to download the dataset as specified

    References:
    - http://www.opengeospatial.org/standards/wfs
    - http://docs.geoserver.org/stable/en/user/services/wfs/vendor.html
    - http://docs.geoserver.org/latest/en/user/tutorials/cql/cql_tutorial.html
    """
    # validate the table name and find out how many features it holds
    table = validate_name(dataset)
    n = bcdata.get_count(table, query=query)
    # if count not provided or if it is greater than n of total features,
    # set count to number of features
    if not count or count > n:
        count = n
    log.info(f"Total features requested: {count}")

    wfs_schema = WFS.get_schema("pub:" + table)
    geom_column = wfs_schema["geometry_column"]
    # DataBC WFS getcapabilities says that it supports paging,
    # and the spec says that responses should include 'next URI'
    # (section 7.7.4.4.1)....
    # But I do not see any next uri in the responses. Instead of following
    # the paged urls, for datasets with >10k records, just generate urls
    # based on number of features in the dataset.
    chunks = math.ceil(count / pagesize)

    # if making several requests, we need to sort by something
    if chunks > 1 and not sortby:
        sortby = get_sortkey(table, wfs_schema)

    # build the request parameters for each chunk
    urls = []
    for i in range(chunks):
        request = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeName": table,
            "outputFormat": "json",
            "SRSNAME": crs,
        }
        if sortby:
            request["sortby"] = sortby.upper()
        # build the CQL based on query and bounds
        # (the bbox param shortcut is mutually exclusive with CQL_FILTER)
        if query and not bounds:
            request["CQL_FILTER"] = query
        if bounds:
            b0, b1, b2, b3 = [str(b) for b in bounds]
            bnd_query = f"bbox({geom_column}, {b0}, {b1}, {b2}, {b3}, '{bounds_crs}')"
            if not query:
                request["CQL_FILTER"] = bnd_query
            else:
                request["CQL_FILTER"] = query + " AND " + bnd_query
        if chunks == 1:
            request["count"] = count
        if chunks > 1:
            request["startIndex"] = i * pagesize
            if count < (request["startIndex"] + pagesize):
                request["count"] = count - request["startIndex"]
            else:
                request["count"] = pagesize
        urls.append(WFS_URL + "?" + urlencode(request, doseq=True))
    return urls


def get_data(
    dataset,
    query=None,
    crs="epsg:4326",
    bounds=None,
    bounds_crs="epsg:3005",
    count=None,
    sortby=None,
    pagesize=10000,
    max_workers=2,
    as_gdf=False,
):
    """Get GeoJSON featurecollection (or geodataframe) from DataBC WFS"""
    urls = define_requests(
        dataset,
        query=query,
        crs=crs,
        bounds=bounds,
        bounds_crs=bounds_crs,
        count=count,
        sortby=sortby,
        pagesize=pagesize,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(make_request, urls)

    outjson = dict(type="FeatureCollection", features=[])
    for result in results:
        outjson["features"] += result
    if not as_gdf:
        # If output crs is specified, include the crs object in the json
        # But as default, we prefer to default to 4326 and RFC7946 (no crs)
        if crs.lower() != "epsg:4326":
            crs_int = crs.split(":")[1]
            outjson[
                "crs"
            ] = f"""{{"type":"name","properties":{{"name":"urn:ogc:def:crs:EPSG::{crs_int}"}}}}"""
        return outjson
    else:
        if len(outjson["features"]) > 0:
            gdf = gpd.GeoDataFrame.from_features(outjson)
            gdf.crs = {"init": crs}
        else:
            gdf = gpd.GeoDataFrame()
        return gdf


def get_features(
    dataset,
    query=None,
    crs="epsg:4326",
    bounds=None,
    bounds_crs="epsg:3005",
    count=None,
    sortby=None,
    pagesize=10000,
    max_workers=2,
):
    """Yield features from DataBC WFS"""
    urls = define_requests(
        dataset,
        query=query,
        crs=crs,
        bounds=bounds,
        bounds_crs=bounds_crs,
        count=count,
        sortby=sortby,
        pagesize=pagesize,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(make_request, urls):
            for feature in result:
                yield feature


def get_types(dataset, count=10):
    """Return distinct types within the first n features"""
    # validate the table name
    table = validate_name(dataset)
    log.info("Getting feature geometry type")
    # get features and find distinct types where geom is not empty
    features = [f for f in get_features(table, count=count)]
    geom_types = list(
        set([f["geometry"]["type"].upper() for f in features if f["geometry"]])
    )
    if len(geom_types) > 1:
        typestring = ",".join(geom_types)
        log.warning(f"Dataset {dataset} has multiple geometry types: {typestring}")
    # validate the type (shouldn't be necessary)
    for geom_type in geom_types:
        if geom_type not in (
            "POINT",
            "LINESTRING",
            "POLYGON",
            "MULTIPOINT",
            "MULTILINESTRING",
            "MULTIPOLYGON",
        ):
            raise ValueError("Geometry type {geomtype} is not supported")
    # if Z coordinates are supplied, modify the type accordingly
    # (presuming that all )
    # (points and lines only, presumably there are no 3d polygon features)
    for i in range(len(geom_types)):
        if (
            geom_types[i] == "POINT"
            and len(features[0]["geometry"]["coordinates"]) == 3
        ):
            geom_types[i] = "POINTZ"
        if (
            geom_types[i] == "MULTIPOINT"
            and len(features[0]["geometry"]["coordinates"][0]) == 3
        ):
            geom_types[i] = "POINTZ"
        if (
            geom_types[i] == "LINESTRING"
            and len(features[0]["geometry"]["coordinates"][0]) == 3
        ):
            geom_types[i] = "LINESTRINGZ"
        if (
            geom_types[i] == "MULTILINESTRING"
            and len(features[0]["geometry"]["coordinates"][0][0]) == 3
        ):
            geom_types[i] = "MULTILINESTRINGZ"
    return geom_types
