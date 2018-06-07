#!/usr/bin/env python
# -*- coding: utf8 -*-

__author__ = "Screenly, Inc"
__copyright__ = "Copyright 2012-2017, Screenly, Inc"
__license__ = "Dual License: GPLv2 and Commercial License"

from datetime import timedelta
from dateutil import parser as date_parser
from functools import wraps
from hurry.filesize import size
import json
from mimetypes import guess_type
from os import getenv, makedirs, mkdir, path, remove, rename, statvfs
from pwgen import pwgen
import sh
from sh import git
from subprocess import check_output
from time import sleep
import traceback
import uuid

from flask import Flask, make_response, render_template, request, send_from_directory
from flask_cors import CORS
from flask_restful_swagger_2 import Api, Resource, Schema, swagger
from flask_swagger_ui import get_swaggerui_blueprint

from gunicorn.app.base import Application
from werkzeug.wrappers import Request

from lib import assets_helper
from lib import backup_helper
from lib import db
from lib import diagnostics
from lib import queries

from lib.utils import get_node_ip
from lib.utils import get_video_duration
from lib.utils import download_video_from_youtube, json_dump
from lib.utils import url_fails
from lib.utils import validate_url

from settings import auth_basic, CONFIGURABLE_SETTINGS, DEFAULTS, LISTEN, PORT, settings, ZmqPublisher


app = Flask(__name__)
CORS(app)
api = Api(app, api_version="v1", title="Screenly OSE API")


################################
# Utilities
################################

@api.representation('application/json')
def output_json(data, code, headers=None):
    response = make_response(json_dump(data), code)
    response.headers.extend(headers or {})
    return response


def api_error(error):
    return make_response(json_dump({'error': error}), 500)


def is_up_to_date():
    """
    Determine if there is any update available.
    Used in conjunction with check_update() in viewer.py.
    """

    sha_file = path.join(settings.get_configdir(), 'latest_screenly_sha')

    # Until this has been created by viewer.py,
    # let's just assume we're up to date.
    if not path.exists(sha_file):
        return True

    try:
        with open(sha_file, 'r') as f:
            latest_sha = f.read().strip()
    except:
        latest_sha = None

    if latest_sha:
        branch_sha = git('rev-parse', 'HEAD')
        return branch_sha.stdout.strip() == latest_sha

    # If we weren't able to verify with remote side,
    # we'll set up_to_date to true in order to hide
    # the 'update available' message
    else:
        return True


def template(template_name, **context):
    """Screenly template response generator. Shares the
    same function signature as Flask's render_template() method
    but also injects some global context."""

    # Add global contexts
    context['up_to_date'] = is_up_to_date()
    context['default_duration'] = settings['default_duration']
    context['default_streaming_duration'] = settings['default_streaming_duration']
    context['use_24_hour_clock'] = settings['use_24_hour_clock']
    context['template_settings'] = {
        'imports': ['from lib.utils import template_handle_unicode'],
        'default_filters': ['template_handle_unicode'],
    }

    return render_template(template_name, context=context)


################################
# Models
################################

class AssetModel(Schema):
    type = 'object'
    properties = {
        'asset_id': {'type': 'string'},
        'name': {'type': 'string'},
        'uri': {'type': 'string'},
        'start_date': {
            'type': 'string',
            'format': 'date-time'
        },
        'end_date': {
            'type': 'string',
            'format': 'date-time'
        },
        'duration': {'type': 'string'},
        'mimetype': {'type': 'string'},
        'is_active': {'type': 'boolean'},
        'is_enabled': {
            'type': 'integer',
            'format': 'int64',
        },
        'is_processing': {
            'type': 'integer',
            'format': 'int64',
        },
        'nocache': {
            'type': 'integer',
            'format': 'int64',
        },
        'play_order': {
            'type': 'integer',
            'format': 'int64',
        }
    }


################################
# API
################################

def prepare_asset(request):
    req = Request(request.environ)
    data = None

    # For backward compatibility
    try:
        data = json.loads(req.data)
    except ValueError:
        data = json.loads(req.form['model'])
    except TypeError:
        data = json.loads(req.form['model'])

    def get(key):
        val = data.get(key, '')
        if isinstance(val, unicode):
            return val.strip()
        elif isinstance(val, basestring):
            return val.strip().decode('utf-8')
        else:
            return val

    if not all([get('name'), get('uri'), get('mimetype')]):
        raise Exception("Not enough information provided. Please specify 'name', 'uri', and 'mimetype'.")

    asset = {
        'name': get('name'),
        'mimetype': get('mimetype'),
        'asset_id': get('asset_id'),
        'is_enabled': get('is_enabled'),
        'is_processing': get('is_processing'),
        'nocache': get('nocache'),
    }

    uri = get('uri')

    if uri.startswith('/'):
        if not path.isfile(uri):
            raise Exception("Invalid file path. Failed to add asset.")
    else:
        if not validate_url(uri):
            raise Exception("Invalid URL. Failed to add asset.")

    if not asset['asset_id']:
        asset['asset_id'] = uuid.uuid4().hex
        if uri.startswith('/'):
            rename(uri, path.join(settings['assetdir'], asset['asset_id']))
            uri = path.join(settings['assetdir'], asset['asset_id'])

    if 'youtube_asset' in asset['mimetype']:
        uri, asset['name'], asset['duration'] = download_video_from_youtube(uri, asset['asset_id'])
        asset['mimetype'] = 'video'
        asset['is_processing'] = 1

    asset['uri'] = uri

    if "video" in asset['mimetype']:
        if get('duration') == 'N/A' or int(get('duration')) == 0:
            asset['duration'] = int(get_video_duration(uri).total_seconds())
    else:
        # Crashes if it's not an int. We want that.
        asset['duration'] = int(get('duration'))

    # parse date via python-dateutil and remove timezone info
    if get('start_date'):
        asset['start_date'] = date_parser.parse(get('start_date')).replace(tzinfo=None)
    else:
        asset['start_date'] = ""

    if get('end_date'):
        asset['end_date'] = date_parser.parse(get('end_date')).replace(tzinfo=None)
    else:
        asset['end_date'] = ""

    return asset


# api view decorator. handles errors
def api_response(view):
    @wraps(view)
    def api_view(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except Exception as e:
            traceback.print_exc()
            return api_error(unicode(e))
    return api_view


class Assets(Resource):
    method_decorators = [auth_basic]

    @swagger.doc({
        'responses': {
            '200': {
                'description': 'List of assets',
                'schema': {
                    'type': 'array',
                    'items': AssetModel

                }
            }
        }
    })
    def get(self):
        with db.conn(settings['database']) as conn:
            assets = assets_helper.read(conn)
            return assets

    @api_response
    @swagger.doc({
        'parameters': [
            {
                'name': 'model',
                'in': 'formData',
                'type': 'string',
                'description':
                    '''
                    Yes, that is just a string of JSON not JSON itself it will be parsed on the other end.
                    Content-Type: application/x-www-form-urlencoded
                    model: "{
                        "name": "Website",
                        "mimetype": "webpage",
                        "uri": "http://example.com",
                        "is_active": false,
                        "start_date": "2017-02-02T00:33:00.000Z",
                        "end_date": "2017-03-01T00:33:00.000Z",
                        "duration": "10",
                        "is_enabled": 0,
                        "is_processing": 0,
                        "nocache": 0,
                        "play_order": 0
                    }"
                    '''
            }
        ],
        'responses': {
            '201': {
                'description': 'Asset created',
                'schema': AssetModel
            }
        }
    })
    def post(self):
        asset = prepare_asset(request)
        if url_fails(asset['uri']):
            raise Exception("Could not retrieve file. Check the asset URL.")
        with db.conn(settings['database']) as conn:
            return assets_helper.create(conn, asset), 201


class Asset(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'parameters': [
            {
                'name': 'asset_id',
                'type': 'string',
                'in': 'path',
                'description': 'id of an asset'
            }
        ],
        'responses': {
            '200': {
                'description': 'Asset',
                'schema': AssetModel
            }
        }
    })
    def get(self, asset_id):
        with db.conn(settings['database']) as conn:
            return assets_helper.read(conn, asset_id)

    @swagger.doc({
        'parameters': [
            {
                'name': 'asset_id',
                'type': 'string',
                'in': 'path',
                'description': 'id of an asset'
            },
            {
                'name': 'model',
                'in': 'formData',
                'type': 'string',
                'description':
                    '''
                    Content-Type: application/x-www-form-urlencoded
                    model: "{
                        "asset_id": "793406aa1fd34b85aa82614004c0e63a",
                        "name": "Website",
                        "mimetype": "webpage",
                        "uri": "http://example.com",
                        "is_active": false,
                        "start_date": "2017-02-02T00:33:00.000Z",
                        "end_date": "2017-03-01T00:33:00.000Z",
                        "duration": "10",
                        "is_enabled": 0,
                        "is_processing": 0,
                        "nocache": 0,
                        "play_order": 0
                    }"
                    '''
            }
        ],
        'responses': {
            '200': {
                'description': 'Asset updated',
                'schema': AssetModel
            }
        }
    })
    def put(self, asset_id):
        with db.conn(settings['database']) as conn:
            return assets_helper.update(conn, asset_id, prepare_asset(request))

    @swagger.doc({
        'parameters': [
            {
                'name': 'asset_id',
                'type': 'string',
                'in': 'path',
                'description': 'id of an asset'
            },
        ],
        'responses': {
            '204': {
                'description': 'Deleted'
            }
        }
    })
    def delete(self, asset_id):
        with db.conn(settings['database']) as conn:
            asset = assets_helper.read(conn, asset_id)
            try:
                if asset['uri'].startswith(settings['assetdir']):
                    remove(asset['uri'])
            except OSError:
                pass
            assets_helper.delete(conn, asset_id)
            return '', 204  # return an OK with no content


class AssetsNewVersion(Resource):
    method_decorators = [auth_basic]

    @swagger.doc({
        'responses': {
            '200': {
                'description': 'List of assets',
                'schema': {
                    'type': 'array',
                    'items': AssetModel

                }
            }
        }
    })
    def get(self):
        with db.conn(settings['database']) as conn:
            assets = assets_helper.read(conn)
            return assets

    @api_response
    @swagger.doc({
        'parameters': [
            {
                'in': 'body',
                'name': 'model',
                'description': 'Adds a asset',
                'schema': AssetModel,
                'required': True
            }
        ],
        'responses': {
            '201': {
                'description': 'Asset created',
                'schema': AssetModel
            }
        }
    })
    def post(self):
        asset = prepare_asset(request)
        if url_fails(asset['uri']):
            raise Exception("Could not retrieve file. Check the asset URL.")
        with db.conn(settings['database']) as conn:
            return assets_helper.create(conn, asset), 201


class AssetNewVersion(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'parameters': [
            {
                'name': 'asset_id',
                'type': 'string',
                'in': 'path',
                'description': 'id of an asset'
            }
        ],
        'responses': {
            '200': {
                'description': 'Asset',
                'schema': AssetModel
            }
        }
    })
    def get(self, asset_id):
        with db.conn(settings['database']) as conn:
            return assets_helper.read(conn, asset_id)

    @swagger.doc({
        'parameters': [
            {
                'name': 'asset_id',
                'type': 'string',
                'in': 'path',
                'description': 'id of an asset',
                'required': True
            },
            {
                'in': 'body',
                'name': 'model',
                'description': 'Adds a asset',
                'schema': AssetModel,
                'required': True
            }
        ],
        'responses': {
            '200': {
                'description': 'Asset updated',
                'schema': AssetModel
            }
        }
    })
    def put(self, asset_id):
        with db.conn(settings['database']) as conn:
            return assets_helper.update(conn, asset_id, prepare_asset(request))

    @swagger.doc({
        'parameters': [
            {
                'name': 'asset_id',
                'type': 'string',
                'in': 'path',
                'description': 'id of an asset',
                'required': True

            },
        ],
        'responses': {
            '204': {
                'description': 'Deleted'
            }
        }
    })
    def delete(self, asset_id):
        with db.conn(settings['database']) as conn:
            asset = assets_helper.read(conn, asset_id)
            try:
                if asset['uri'].startswith(settings['assetdir']):
                    remove(asset['uri'])
            except OSError:
                pass
            assets_helper.delete(conn, asset_id)
            return '', 204  # return an OK with no content


class FileAsset(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'parameters': [
            {
                'name': 'file_upload',
                'type': 'file',
                'in': 'formData',
                'description': 'File to be sent'
            }
        ],
        'responses': {
            '200': {
                'description': 'File path',
                'schema': {
                    'type': 'string'
                }
            }
        }
    })
    def post(self):
        req = Request(request.environ)
        file_upload = req.files.get('file_upload')
        filename = file_upload.filename
        file_path = path.join(settings['assetdir'], filename) + ".tmp"

        if 'Content-Range' in request.headers:
            range_str = request.headers['Content-Range']
            start_bytes = int(range_str.split(' ')[1].split('-')[0])
            with open(file_path, 'a') as f:
                f.seek(start_bytes)
                f.write(file_upload.read())
        else:
            file_upload.save(file_path)

        return file_path


class PlaylistOrder(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'parameters': [
            {
                'name': 'ids',
                'in': 'formData',
                'type': 'string',
                'description':
                    '''
                    Content-Type: application/x-www-form-urlencoded
                    ids: "793406aa1fd34b85aa82614004c0e63a,1c5cfa719d1f4a9abae16c983a18903b,9c41068f3b7e452baf4dc3f9b7906595"
                    comma separated ids
                    '''
            },
        ],
        'responses': {
            '204': {
                'description': 'Sorted'
            }
        }
    })
    def post(self):
        with db.conn(settings['database']) as conn:
            assets_helper.save_ordering(conn, request.form.get('ids', '').split(','))


class Backup(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'responses': {
            '200': {
                'description': 'Backup filename',
                'schema': {
                    'type': 'string'
                }
            }
        }
    })
    def post(self):
        filename = backup_helper.create_backup()
        return filename, 201


class Recover(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'parameters': [
            {
                'name': 'backup_upload',
                'type': 'file',
                'in': 'formData'
            }
        ],
        'responses': {
            '200': {
                'description': 'Recovery successful'
            }
        }
    })
    def post(self):
        req = Request(request.environ)
        file_upload = (req.files['backup_upload'])
        filename = file_upload.filename

        if guess_type(filename)[0] != 'application/x-tar':
            raise Exception("Incorrect file extension.")

        location = path.join("static", filename)
        file_upload.save(location)
        backup_helper.recover(location)
        return "Recovery successful."


class ResetWifiConfig(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'responses': {
            '204': {
                'description': 'Deleted'
            }
        }
    })
    def get(self):
        home = getenv('HOME')
        file_path = path.join(home, '.screenly/initialized')

        if path.isfile(file_path):
            remove(file_path)

        return '', 204


class Info(Resource):
    method_decorators = [api_response, auth_basic]

    def get(self):
        viewlog = None
        try:
            viewlog = [line.decode('utf-8') for line in
                       check_output(['sudo', 'systemctl', 'status', 'screenly-viewer.service', '-n', '20']).split('\n')]
        except:
            pass

        # Calculate disk space
        slash = statvfs("/")
        free_space = size(slash.f_bavail * slash.f_frsize)

        return {
            'viewlog': viewlog,
            'loadavg': diagnostics.get_load_avg()['15 min'],
            'free_space': free_space,
            'display_info': diagnostics.get_monitor_status(),
            'display_power': diagnostics.get_display_power()
        }


class AssetsControl(Resource):
    method_decorators = [api_response, auth_basic]

    @swagger.doc({
        'parameters': [
            {
                'name': 'command',
                'type': 'string',
                'in': 'path',
                'description':
                    '''
                    Control commands:
                    next - show next asset
                    previous - show previous asset
                    asset&asset_id - show asset with `asset_id` id
                    '''
            }
        ],
        'responses': {
            '200': {
                'description': 'Asset switched'
            }
        }
    })
    def get(self, command):
        publisher = ZmqPublisher.get_instance()
        publisher.send_to_viewer(command)
        return "Asset switched"

api.add_resource(Assets, '/api/v1/assets')
api.add_resource(Asset, '/api/v1/assets/<asset_id>')
api.add_resource(AssetsNewVersion, '/api/v1.1/assets')
api.add_resource(AssetNewVersion, '/api/v1.1/assets/<asset_id>')
api.add_resource(FileAsset, '/api/v1/file_asset')
api.add_resource(PlaylistOrder, '/api/v1/assets/order')
api.add_resource(Backup, '/api/v1/backup')
api.add_resource(Recover, '/api/v1/recover')
api.add_resource(AssetsControl, '/api/v1/assets/control/<command>')
api.add_resource(Info, '/api/v1/info')
api.add_resource(ResetWifiConfig, '/api/v1/reset_wifi')

try:
    my_ip = get_node_ip()
except:
    pass
else:
    SWAGGER_URL = '/api/docs'
    swagger_address = getenv("SWAGGER_HOST", my_ip)

    if settings['use_ssl']:
        API_URL = 'https://{}/api/swagger.json'.format(swagger_address)
    elif LISTEN == '127.0.0.1' or swagger_address != my_ip:
        API_URL = "http://{}/api/swagger.json".format(swagger_address)
    else:
        API_URL = "http://{}:{}/api/swagger.json".format(swagger_address, PORT)

    swaggerui_blueprint = get_swaggerui_blueprint(
        SWAGGER_URL,
        API_URL,
        config={
            'app_name': "Screenly API"
        }
    )
    app.register_blueprint(swaggerui_blueprint, url_prefix=SWAGGER_URL)


################################
# Views
################################


@app.route('/')
@auth_basic
def viewIndex():
    player_name = settings['player_name']
    my_ip = get_node_ip()
    resin_uuid = getenv("RESIN_UUID", None)

    ws_addresses = []

    if settings['use_ssl']:
        ws_addresses.append('wss://' + my_ip + '/ws/')
    else:
        ws_addresses.append('ws://' + my_ip + ':' + settings['websocket_port'])

    if resin_uuid:
        ws_addresses.append('wss://{}.resindevice.io/ws/'.format(resin_uuid))

    return template('index.html', ws_addresses=ws_addresses, player_name=player_name)


@app.route('/settings', methods=["GET", "POST"])
@auth_basic
def settings_page():

    context = {'flash': None}

    if request.method == "POST":
        try:
            for field, default in CONFIGURABLE_SETTINGS.items():
                value = request.form.get(field, default)
                if field == "password":
                    if value != request.form.get('password2', ''):
                        raise ValueError("Passwords do not match.")
                    if value == "" and settings['user'] != "":
                        value = settings['password']

                if isinstance(default, bool):
                    value = value == 'on'
                settings[field] = value

            settings.save()
            publisher = ZmqPublisher.get_instance()
            publisher.send_to_viewer('reload')
            context['flash'] = {'class': "success", 'message': "Settings were successfully saved."}
        except ValueError as e:
            context['flash'] = {'class': "error", 'message': e}
        except IOError as e:
            context['flash'] = {'class': "error", 'message': e}
        except OSError as e:
            context['flash'] = {'class': "error", 'message': e}
    else:
        settings.load()
    for field, default in DEFAULTS['viewer'].items():
        context[field] = settings[field]

    context['user'] = settings['user']

    if not settings['user'] or not settings['password']:
        context['use_auth'] = False
    else:
        context['use_auth'] = True

    return template('settings.html', **context)


@app.route('/system_info')
@auth_basic
def system_info():
    viewlog = None
    try:
        viewlog = [line.decode('utf-8') for line in
                   check_output(['sudo', 'systemctl', 'status', 'screenly-viewer.service', '-n', '20']).split('\n')]
    except:
        pass

    loadavg = diagnostics.get_load_avg()['15 min']

    display_info = diagnostics.get_monitor_status()

    display_power = diagnostics.get_display_power()

    # Calculate disk space
    slash = statvfs("/")
    free_space = size(slash.f_bavail * slash.f_frsize)

    # Get uptime
    uptime_in_seconds = diagnostics.get_uptime()
    system_uptime = timedelta(seconds=uptime_in_seconds)

    # Player name for title
    player_name = settings['player_name']

    return template(
        'system_info.html',
        player_name=player_name,
        viewlog=viewlog,
        loadavg=loadavg,
        free_space=free_space,
        uptime=system_uptime,
        display_info=display_info,
        display_power=display_power
    )


@app.route('/splash_page')
def splash_page():
    url = None
    try:
        my_ip = get_node_ip()
    except Exception as e:
        ip_lookup = False
        error_msg = e
    else:
        ip_lookup = True

        if settings['use_ssl']:
            url = 'https://{}'.format(my_ip)
        elif LISTEN == '127.0.0.1':
            url = "http://{}".format(my_ip)
        else:
            url = "http://{}:{}".format(my_ip, PORT)

    msg = url if url else error_msg
    return template('splash_page.html', ip_lookup=ip_lookup, msg=msg)


@app.errorhandler(403)
def mistake403(code):
    return 'The parameter you passed has the wrong format!'


@app.errorhandler(404)
def mistake404(code):
    return 'Sorry, this page does not exist!'

################################
# Static
################################


@app.route('/static_with_mime/<string:path>')
def static_with_mime(path):
    mimetype = request.args['mime'] if 'mime' in request.args else 'auto'
    return send_from_directory(directory='static', filename=path, mimetype=mimetype)


if __name__ == "__main__":
    # Make sure the asset folder exist. If not, create it
    if not path.isdir(settings['assetdir']):
        mkdir(settings['assetdir'])
    # Create config dir if it doesn't exist
    if not path.isdir(settings.get_configdir()):
        makedirs(settings.get_configdir())

    with db.conn(settings['database']) as conn:
        with db.cursor(conn) as cursor:
            cursor.execute(queries.exists_table)
            if cursor.fetchone() is None:
                cursor.execute(assets_helper.create_assets_table)

    config = {
        'bind': '{}:{}'.format(LISTEN, PORT),
        'threads': 2,
        'timeout': 20
    }

    class GunicornApplication(Application):
        def init(self, parser, opts, args):
            return config

        def load(self):
            return app

    GunicornApplication().run()
