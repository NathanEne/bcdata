import json
import logging
import math
import os
import re
import subprocess
from subprocess import Popen
from urllib.parse import urlencode
from urllib.parse import urlparse

import click
from cligj import indent_opt
from cligj import compact_opt
from owslib.wfs import WebFeatureService

import pgdata

import bcdata


bcdata.configure_logging()
log = logging.getLogger(__name__)


def parse_db_url(db_url):
    """provided a db url, return a dict with connection properties
    """
    u = urlparse(db_url)
    db = {}
    db["database"] = u.path[1:]
    db["user"] = u.username
    db["password"] = u.password
    db["host"] = u.hostname
    db["port"] = u.port
    return db


def get_objects(ctx, args, incomplete):
    return [k for k in bcdata.list_tables() if incomplete in k]


# bounds handling direct from rasterio
# https://github.com/mapbox/rasterio/blob/master/rasterio/rio/options.py
# https://github.com/mapbox/rasterio/blob/master/rasterio/rio/clip.py


def from_like_context(ctx, param, value):
    """Return the value for an option from the context if the option
    or `--all` is given, else return None."""
    if ctx.obj and ctx.obj.get("like") and (value == "like" or ctx.obj.get("all_like")):
        return ctx.obj["like"][param.name]
    else:
        return None


def bounds_handler(ctx, param, value):
    """Handle different forms of bounds."""
    retval = from_like_context(ctx, param, value)
    if retval is None and value is not None:
        try:
            value = value.strip(", []")
            retval = tuple(float(x) for x in re.split(r"[,\s]+", value))
            assert len(retval) == 4
            return retval
        except Exception:
            raise click.BadParameter(
                "{0!r} is not a valid bounding box representation".format(value)
            )
    else:  # pragma: no cover
        return retval


bounds_opt = click.option(
    "--bounds",
    default=None,
    callback=bounds_handler,
    help='Bounds: "left bottom right top" or "[left, bottom, right, top]".',
)

bounds_opt_required = click.option(
    "--bounds",
    required=True,
    default=None,
    callback=bounds_handler,
    help='Bounds: "left bottom right top" or "[left, bottom, right, top]".',
)

dst_crs_opt = click.option("--dst-crs", "--dst_crs", help="Destination CRS.")


@click.group()
def cli():
    pass


@cli.command()
@click.option("--refresh", "-r", is_flag=True, help="Refresh the cached list")
def list(refresh):
    """List DataBC layers available via WFS
    """
    # This works too, but is much slower:
    # ogrinfo WFS:http://openmaps.gov.bc.ca/geo/ows?VERSION=1.1.0
    for table in bcdata.list_tables(refresh):
        click.echo(table)


@cli.command()
@click.argument("dataset", type=click.STRING, autocompletion=get_objects)
@indent_opt
# Options to pick out a single metadata item and print it as
# a string.
@click.option(
    "--count", "meta_member", flag_value="count", help="Print the count of features."
)
@click.option(
    "--name", "meta_member", flag_value="name", help="Print the datasource's name."
)
def info(dataset, indent, meta_member):
    """Print basic metadata about a DataBC WFS layer as JSON.

    Optionally print a single metadata item as a string.
    """
    table = bcdata.validate_name(dataset)
    wfs = WebFeatureService(url=bcdata.OWS_URL, version="2.0.0")
    info = {}
    info["name"] = table
    info["count"] = bcdata.get_count(table)
    info["schema"] = wfs.get_schema("pub:" + table)
    if meta_member:
        click.echo(info[meta_member])
    else:
        click.echo(json.dumps(info, indent=indent))


@cli.command()
@click.option("--out_file", "-o", help="Output file", default="dem25.tif")
@bounds_opt_required
@dst_crs_opt
@click.option("--resolution", "-r", type=int, default=25)
def dem(bounds, dst_crs, out_file, resolution):
    """Dump BC DEM to TIFF
    """
    if not dst_crs:
        dst_crs = 'EPSG:3005'
    bcdata.get_dem(bounds, dst_crs=dst_crs, out_file=out_file, resolution=resolution)


@cli.command()
@click.argument("dataset", type=click.STRING, autocompletion=get_objects)
@click.option(
    "--query",
    help="A valid CQL or ECQL query, quote enclosed (https://docs.geoserver.org/stable/en/user/tutorials/cql/cql_tutorial.html)",
)
@click.option("--out_file", "-o", help="Output file")
@bounds_opt
def dump(dataset, query, out_file, bounds):
    """Write DataBC features to stdout as GeoJSON feature collection.

    \b
      $ bcdata dump bc-airports
      $ bcdata dump bc-airports --query "AIRPORT_NAME='Victoria Harbour (Shoal Point) Heliport'"
      $ bcdata dump bc-airports --bounds xmin ymin xmax ymax

    The values of --bounds must be in BC Albers.

     It can also be combined to read bounds of a feature dataset using Fiona:
    \b
      $ bcdata dump bc-airports --bounds $(fio info aoi.shp --bounds)

    """
    table = bcdata.validate_name(dataset)
    data = bcdata.get_data(table, query=query, bounds=bounds)
    if out_file:
        with open(out_file, "w") as f:
            json.dump(data.json(), f)
    else:
        sink = click.get_text_stream("stdout")
        sink.write(json.dumps(data))


@cli.command()
@click.argument("dataset", type=click.STRING, autocompletion=get_objects)
@click.option(
    "--query",
    help="A valid `CQL` or `ECQL` query (https://docs.geoserver.org/stable/en/user/tutorials/cql/cql_tutorial.html)",
)
@indent_opt
@bounds_opt
@compact_opt
@dst_crs_opt
@click.option(
    "--pagesize", "-p", default=10000, help="Max number of records to request"
)
@click.option("--sortby", "-s", help="Name of sort field")
def cat(dataset, query, bounds, indent, compact, dst_crs, pagesize, sortby):
    """Write DataBC features to stdout as GeoJSON feature objects.
    """
    # Note that cat does not concatenate!
    dump_kwds = {"sort_keys": True}
    if indent:
        dump_kwds["indent"] = indent
    if compact:
        dump_kwds["separators"] = (",", ":")
    table = bcdata.validate_name(dataset)
    for feat in bcdata.get_features(table, query=query, bounds=bounds):
        click.echo(json.dumps(feat, **dump_kwds))


@cli.command()
@click.argument("dataset", type=click.STRING, autocompletion=get_objects)
@click.option(
    "--db_url",
    "-db",
    help="SQLAlchemy database url",
    default=os.environ["DATABASE_URL"],
)
@click.option(
    "--query",
    help="A valid `CQL` or `ECQL` query (https://docs.geoserver.org/stable/en/user/tutorials/cql/cql_tutorial.html)",
)
@click.option(
    "--pagesize", "-p", default=10000, help="Max number of records to request"
)
@click.option("--sortby", "-s", help="Name of sort field")
def bc2pg(dataset, db_url, query, pagesize, sortby):
    """Download a DataBC WFS layer to postgres - an ogr2ogr wrapper.

     \b
      $ bcdata bc2pg bc-airports --db_url postgresql://postgres:postgres@localhost:5432/postgis

    The default target database can be specified by setting the $DATABASE_URL
    environment variable.
    https://docs.sqlalchemy.org/en/latest/core/engines.html#database-urls
    """

    src_table = bcdata.validate_name(dataset)
    schema, table = [i.lower() for i in src_table.split(".")]

    # create schema if it does not exist
    conn = pgdata.connect(db_url)
    if schema not in conn.schemas:
        click.echo("Schema {} does not exist, creating it".format(schema))
        conn.create_schema(schema)

    # build parameters for each required request
    param_dicts = bcdata.define_request(dataset, query=query, sortby=sortby, pagesize=10000)

    # run the first request / load
    payload = urlencode(param_dicts[0], doseq=True)
    url = bcdata.WFS_URL + "?" + payload
    db = parse_db_url(db_url)
    command = [
        "ogr2ogr",
        "-f PostgreSQL",
        'PG:"host={h} user={u} dbname={db} password={pwd}"'.format(
            h=db["host"], u=db["user"], db=db["database"], pwd=db["password"]
        ),
        "-t_srs EPSG:3005",
        "-lco OVERWRITE=YES",
        "-lco SCHEMA={}".format(schema),
        "-lco GEOMETRY_NAME=geom",
        "-nln {}".format(table),
        '"' + url + '"',
    ]
    click.echo("Loading {} to {}".format(src_table, db_url))
    subprocess.call(" ".join(command), shell=True)

    # build commands for the rest of the chunks
    if len(param_dicts) > 1:
        commands = []
        for chunk, paramdict in enumerate(param_dicts[1:]):
            payload = urlencode(paramdict, doseq=True)
            url = bcdata.WFS_URL + "?" + payload
            command = [
                "ogr2ogr",
                "-update",
                "-append",
                "-f PostgreSQL",
                # note that schema must be specified here in connection
                # string when appending to a layer, -lco opts are ignored
                'PG:"host={h} user={u} dbname={db} password={pwd} active_schema={s}"'.format(
                    h=db["host"],
                    u=db["user"],
                    db=db["database"],
                    pwd=db["password"],
                    s=schema,
                ),
                "-t_srs EPSG:3005",
                "-nln {}".format(table),
                '"' + url + '"',
            ]
            commands.append(" ".join(command))
        # now execute in parallel
        click.echo("Loading remaining chunks in parallel")
        procs_list = [Popen(cmd, shell=True) for cmd in commands]
        for proc in procs_list:
            proc.wait()

    # todo - add a check to make sure feature counts add up
    click.echo("Load of {} to {} complete".format(src_table, db_url))
