#!/usr/bin/env python3
"""CSP media client. Requirements: R01-R15, R17-R18. Python 3.9+, stdlib only."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class Failure(Exception):
    def __init__(self, code, message, exit_code=6, missing=None):
        self.code, self.message, self.exit_code = code, message, exit_code
        self.missing = missing


def clarify(fields):
    raise Failure('clarification', '请补充或确认必要输入；未提交媒体请求。', 2, fields)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def url_check(value, public=False):
    if not isinstance(value, str):
        clarify(['public_url'])
    try:
        p = urllib.parse.urlsplit(value)
        _ = p.port  # Validate an explicitly supplied port number.
    except ValueError:
        clarify(['public_url'])
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.fragment:
        clarify(['public_url'])
    if public:
        host = p.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local')):
            clarify(['public_url'])
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            clarify(['public_url'])
    return p


def loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def positive(value, integer=False):
    return (not isinstance(value, bool) and isinstance(value, int if integer else (int, float))
            and value > 0 and value < float('inf'))


class Client:
    def __init__(self):
        self.key = os.environ.get('CSP_API_KEY', '')
        if not self.key:
            raise Failure('missing_key', '请在环境中配置 CSP_API_KEY。', 3)
        if '\n' in self.key or '\r' in self.key:
            raise Failure('configuration_error', 'CSP_API_KEY 格式无效。')
        self.base = os.environ.get('CSP_BASE_URL', 'https://llm.zerohalu.com/v1').rstrip('/')
        p = url_check(self.base)
        if p.query or not p.path.endswith('/v1') or (p.scheme != 'https' and not loopback(p.hostname)):
            raise Failure('configuration_error', 'CSP_BASE_URL 必须为 HTTPS /v1 基址；HTTP 仅支持本地回环。')
        self.origin = urllib.parse.urlunsplit((p.scheme, p.netloc, '', '', ''))
        self.local_api = p.scheme == 'http' and loopback(p.hostname)
        self.opener = urllib.request.build_opener(NoRedirect())

    def guard(self, value):
        if self.key in json.dumps(value, ensure_ascii=False):
            raise Failure('credential_exposure', '检测到凭证出现在非认证内容中，已拒绝处理。')

    def fetch(self, url, payload=None, api=True):
        self.guard(url)
        url_check(url)
        headers = {}
        body = None
        if api:
            # Only internally constructed API URLs are authorized, never redirects.
            p = urllib.parse.urlsplit(url)
            origin = urllib.parse.urlunsplit((p.scheme, p.netloc, '', '', ''))
            if origin != self.origin:
                raise Failure('configuration_error', '认证目标与配置主机不一致。')
            headers['Authorization'] = 'Bearer ' + self.key
        if payload is not None:
            self.guard(payload)
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as response:
                data = response.read()
                mime = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise Failure('api_error' if api else 'download_error', 'HTTP 状态码 %d；未自动重试。' % code, 4 if api else 6)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            raise Failure('network_error', '网络失败；提交结果可能不明，未自动重试。' if payload is not None else '网络读取失败；可保留原任务 ID 恢复。', 5)
        if self.key.encode('utf-8') in data:
            raise Failure('credential_exposure', '响应包含凭证，已拒绝输出和保存。')
        return data, mime

    def json_api(self, path, payload=None, root=False):
        raw, _ = self.fetch((self.origin if root else self.base) + path, payload)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise Failure('protocol_error', 'API 未返回有效 JSON。')
        if not isinstance(value, dict):
            raise Failure('protocol_error', 'API JSON 必须为对象。')
        self.guard(value)
        if value.get('error'):
            raise Failure('api_error', 'API 返回错误；服务正文已省略。', 4)
        return value

    def catalog(self):
        models = self.json_api('/models')
        info = self.json_api('/model/info', root=True)
        data = models.get('data')
        if not isinstance(data, list):
            raise Failure('protocol_error', '模型列表缺少 data 数组。')
        aliases = [x['id'] for x in data if isinstance(x, dict) and isinstance(x.get('id'), str)]
        return {'aliases': aliases, 'model_info': info}

    def verify_model(self, model, operation, result, request=None):
        catalog = self.catalog()
        if model not in catalog['aliases']:
            result['discovery'] = catalog
            clarify(['model_alias'])
        rows = catalog['model_info'].get('data', [])
        modes = set()
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or row.get('model_name', row.get('id')) != model:
                    continue
                result['_model_record'] = row
                for source in (row, row.get('model_info', {})):
                    if not isinstance(source, dict):
                        continue
                    supported = source.get('supported_modes', [])
                    if isinstance(supported, list):
                        modes.update(v for v in supported if isinstance(v, str))
                    capabilities = source.get('capabilities', {})
                    if isinstance(capabilities, list):
                        modes.update(v for v in capabilities if isinstance(v, str))
                    elif isinstance(capabilities, dict):
                        # The gateway guide's model/info example uses capability
                        # keys with display-name strings (for example t2v: 文生视频),
                        # not only boolean values.
                        for name, value in capabilities.items():
                            if name in ('t2i', 'i2i', 't2v', 'i2v', 'r2v', 'voice', 'speech', 'tts') and value not in (None, False, '', [], {}):
                                modes.add(name)
                    if isinstance(source.get('mode'), str):
                        modes.add(source['mode'])
        aliases = {
            'voice_cloning': 'voice', 'voice_enrollment': 'voice',
            'audio_voice_enrollment': 'voice', 'enrollment': 'voice',
            'tts': 'speech', 'audio_speech': 'speech',
        }
        modes.update(canonical for name, canonical in aliases.items() if name in modes)
        if operation not in modes:
            result['discovery'] = catalog
            clarify(['model_capability:' + operation])
        if operation == 'voice' and request and 'target_model' in request:
            target_model = request['target_model']
            if target_model not in catalog['aliases']:
                result['discovery'] = catalog
                clarify(['target_model_alias'])

    def xuanling_voice(self, model, record):
        text = (model + ' ' + json.dumps(record, ensure_ascii=False)).lower()
        return any(marker in text for marker in ('xuanling', 'xuanmap-voice', '玄灵'))


def required(req, field):
    value = req.get(field)
    if value is None or value == '' or value == []:
        clarify([field])
    return value


def string(req, field):
    value = required(req, field)
    if not isinstance(value, str) or not value.strip():
        clarify([field])
    return value


def urls(value, field):
    if not isinstance(value, list) or not value:
        clarify([field])
    for item in value:
        url_check(item, public=True)
    return value


def timings(req):
    timeout = req.get('poll_timeout', 120)
    interval = req.get('poll_interval', 2)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout < float('inf'):
        clarify(['poll_timeout'])
    if not positive(interval):
        clarify(['poll_interval'])
    return timeout, interval


def validate(req, polling=False):
    if not isinstance(req, dict):
        clarify(['request_object'])
    op = string(req, 'operation')
    common = {'operation', 'model', 'output_dir', 'poll_timeout', 'poll_interval'}
    specifics = {
        't2i': {'prompt', 'size'}, 'i2i': {'prompt', 'size', 'images'},
        't2v': {'prompt', 'seconds', 'size'}, 'i2v': {'prompt', 'seconds', 'size', 'img_url'},
        'r2v': {'prompt', 'seconds', 'size', 'reference_urls', 'media'},
        'voice': {'reference_audio', 'consent', 'public_pcm_wav_16k_mono', 'name', 'prefix', 'target_model'},
        'speech': {'text', 'voice_id', 'voice_authorized'}
    }
    allowed = common | ({'task_id'} if polling else specifics.get(op, set()) | {'quantity'})
    if (polling and op not in ('image', 'video')) or (not polling and op not in specifics):
        clarify(['supported_operation'])
    if set(req) - allowed:
        clarify(['unknown_fields:' + ','.join(sorted(set(req) - allowed))])
    string(req, 'model')
    string(req, 'output_dir')
    timings(req)
    if polling:
        string(req, 'task_id')
        return None
    quantity = required(req, 'quantity')
    if not positive(quantity, integer=True):
        clarify(['quantity'])
    if op not in ('t2i', 'i2i') and quantity != 1:
        clarify(['quantity=1 per invocation; explicitly requested multiples require separate invocations'])
    payload = {'model': req['model']}
    if op in ('t2i', 'i2i', 't2v', 'i2v', 'r2v'):
        payload['prompt'] = string(req, 'prompt')
    if op in ('t2i', 'i2i'):
        payload['n'] = quantity
        if 'size' in req:
            payload['size'] = string(req, 'size')
        if op == 'i2i':
            payload['extra_body'] = {'images': urls(required(req, 'images'), 'images')}
    if op in ('t2v', 'i2v', 'r2v'):
        seconds = required(req, 'seconds')
        if not positive(seconds):
            clarify(['seconds'])
        payload.update(seconds=seconds, size=string(req, 'size'))
        if op == 'i2v':
            reference = string(req, 'img_url')
            url_check(reference, public=True)
            payload['extra_body'] = {'img_url': reference}
        if op == 'r2v':
            extra = {'gen_mode': 'r2v'}
            if ('reference_urls' in req) == ('media' in req):
                clarify(['exactly_one_of:reference_urls,media'])
            if 'reference_urls' in req:
                extra['reference_urls'] = urls(req['reference_urls'], 'reference_urls')
            else:
                media = required(req, 'media')
                if not isinstance(media, list):
                    clarify(['media'])
                for entry in media:
                    if not isinstance(entry, dict) or set(entry) != {'type', 'url'} or entry['type'] not in ('reference_image', 'reference_video', 'reference_audio'):
                        clarify(['media:type,url'])
                    url_check(entry['url'], public=True)
                extra['media'] = media
            payload['extra_body'] = extra
    if op == 'voice':
        if req.get('consent') is not True:
            clarify(['voice_ownership_or_permission'])
        reference = string(req, 'reference_audio')
        url_check(reference, public=True)
        payload['media'] = [{'type': 'reference_audio', 'url': reference}]
        for optional in ('name', 'prefix', 'target_model'):
            if optional in req:
                payload[optional] = string(req, optional)
    if op == 'speech':
        if req.get('voice_authorized') is not True:
            clarify(['authorized_cloned_voice'])
        payload.update(input=string(req, 'text'), voice=string(req, 'voice_id'))
    return payload


def extension(mime, url, fallback):
    known = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'video/mp4': '.mp4',
             'audio/mpeg': '.mp3', 'audio/wav': '.wav', 'audio/x-wav': '.wav', 'audio/ogg': '.ogg', 'audio/flac': '.flac'}
    if mime in known:
        return known[mime]
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower() if url else ''
    return suffix if suffix in set(known.values()) else fallback


def save(client, data, mime, url, directory, result, fallback):
    if client.key.encode('utf-8') in data:
        raise Failure('credential_exposure', '媒体包含凭证，已拒绝保存。')
    if not data:
        raise Failure('protocol_error', '媒体响应为空。')
    directory = Path(directory).expanduser().resolve()
    destination = directory / ('csp-' + uuid.uuid4().hex + extension(mime, url, fallback))
    created = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as handle:
            created = True
            handle.write(data)
    except OSError:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise Failure('local_io_error', '媒体文件写入失败。')
    result['saved_paths'].append(str(destination))


def artifacts(client, response, kind, req, result):
    if kind == 'video':
        usage = response.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        url = usage.get('video_url') or response.get('video_url') or response.get('url')
        entries = [{'url': url}] if isinstance(url, str) and url else []
    else:
        entries = response.get('data', [])
        if not isinstance(entries, list):
            raise Failure('protocol_error', '图片 data 必须为数组。')
        if not entries and (response.get('url') or response.get('b64_json')):
            entries = [response]
    valid = [e for e in entries if isinstance(e, dict) and (e.get('url') or e.get('b64_json'))]
    if not valid:
        return False
    for entry in valid:
        if entry.get('url'):
            url = entry['url']
            url_check(url, public=not client.local_api)
            result['remote_urls'].append(url)
            data, mime = client.fetch(url, api=False)
            save(client, data, mime, url, req['output_dir'], result, '.mp4' if kind == 'video' else '.png')
        else:
            try:
                data = base64.b64decode(entry['b64_json'], validate=True)
            except (ValueError, TypeError):
                raise Failure('protocol_error', '图片 base64 无效。')
            save(client, data, 'image/png', '', req['output_dir'], result, '.png')
    return True


def consume(client, response, kind, req, result):
    state = str(response.get('status', '')).lower()
    result['api_status'] = state or None
    if state in ('failed', 'error', 'cancelled', 'canceled'):
        raise Failure('task_failed', 'API 报告任务失败。', 4)
    if kind == 'video' and state not in ('completed', 'succeeded', 'success'):
        return False
    if kind == 'image' and state and state not in ('completed', 'succeeded', 'success'):
        return False
    if artifacts(client, response, kind, req, result):
        result['status'] = 'completed'
        return True
    return False


def poll(client, kind, req, result):
    timeout, interval = timings(req)
    deadline = time.monotonic() + timeout
    task_id = result['task_id']
    # Preserve already encoded ID bytes; quote raw path separators without decoding.
    component = urllib.parse.quote(task_id, safe='%')
    route = ('/videos/' if kind == 'video' else '/images/generations/') + component
    while time.monotonic() < deadline:
        response = client.json_api(route)
        if consume(client, response, kind, req, result):
            return 0
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    result['status'] = 'pending'
    return 7


def execute(client, req, result, polling):
    payload = validate(req, polling)
    client.guard(req)
    result['model'] = req['model']
    if polling:
        result['task_id'] = req['task_id']
        return poll(client, req['operation'], req, result)
    op = req['operation']
    client.verify_model(req['model'], op, result, req)
    record = result.pop('_model_record', {})
    if op == 'voice':
        if client.xuanling_voice(req['model'], record) and req.get('public_pcm_wav_16k_mono') is not True:
            clarify(['confirmed_public_PCM_WAV_16000Hz_mono'])
        response = client.json_api('/audio/voices', payload)
        voice_id = response.get('voice_id')
        if not isinstance(voice_id, str) or not voice_id:
            raise Failure('protocol_error', '注册响应缺少 voice_id；不要自动重新注册。')
        result.update(status='completed', voice_id=voice_id)
        return 0
    if op == 'speech':
        data, mime = client.fetch(client.base + '/audio/speech', payload)
        if mime == 'application/json' or mime.endswith('+json'):
            raise Failure('protocol_error', '语音合成未返回音频字节；不要自动重提。')
        save(client, data, mime, '', req['output_dir'], result, '.bin')
        result['status'] = 'completed'
        return 0
    kind = 'image' if op in ('t2i', 'i2i') else 'video'
    response = client.json_api('/images/generations' if kind == 'image' else '/videos', payload)
    task_id = response.get('id')
    if isinstance(task_id, str) and task_id:
        result['task_id'] = task_id
    if consume(client, response, kind, req, result):
        return 0
    if not result['task_id']:
        raise Failure('protocol_error', '响应无可用成果或任务 ID；不要自动重提。')
    return poll(client, kind, req, result)


def main(argv=None):
    result = {'status': 'error', 'model': None, 'task_id': None, 'saved_paths': [], 'remote_urls': []}
    code = 6
    client = None
    try:
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument('command', choices=['models', 'run', 'poll'])
        parser.add_argument('--request')
        try:
            args = parser.parse_args(argv)
        except (argparse.ArgumentError, SystemExit):
            raise Failure('input_error', '用法：models，或 run/poll --request FILE；FILE 可为 -。', 2)
        client = Client()
        if args.command == 'models':
            if args.request:
                clarify(['models_does_not_accept_request'])
            result.update(status='completed', discovery=client.catalog())
            code = 0
        else:
            if not args.request:
                clarify(['request'])
            try:
                if args.request == '-':
                    req = json.load(sys.stdin)
                else:
                    with open(args.request, encoding='utf-8') as handle:
                        req = json.load(handle)
            except (OSError, ValueError, UnicodeError):
                raise Failure('input_error', '无法读取有效 UTF-8 JSON 请求。', 2)
            code = execute(client, req, result, args.command == 'poll')
    except Failure as exc:
        code = exc.exit_code
        result['status'] = 'clarification' if code == 2 else 'error'
        result['error'] = {'code': exc.code, 'message': exc.message}
        if exc.missing:
            result['missing'] = exc.missing
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        code = 6
        result['status'] = 'error'
        result['error'] = {'code': 'client_error', 'message': '客户端无法处理输入或响应；未自动重试。'}
    result.pop('_model_record', None)
    output = json.dumps(result, ensure_ascii=False, allow_nan=False)
    # Final defense also covers user-controlled paths and echoed identifiers.
    key = os.environ.get('CSP_API_KEY', '')
    if key and (key in output or json.dumps(key, ensure_ascii=False)[1:-1] in output):
        output = json.dumps({'status': 'error', 'error': {'code': 'credential_exposure', 'message': '输出已阻止。'}})
        code = 6
    print(output)
    return code


if __name__ == '__main__':
    sys.exit(main())
