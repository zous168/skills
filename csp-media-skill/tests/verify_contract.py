#!/usr/bin/env python3
"""Run the bundled CSP client against a local fake gateway; never calls CSP."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def verify():
    key = secrets.token_urlsafe(32)
    image_bytes = b'csp-local-contract-image'
    video_bytes = b'csp-local-contract-video'
    speech_bytes = b'RIFF-csp-local-contract-speech'
    image_id = 'image%2Fjob%3D7'
    video_id = 'video%2Fjob%3D42'
    observed = []
    handler_errors = []
    client_path = (Path(__file__).resolve().parents[1] / 'dist' / 'csp-media' /
                   'scripts' / 'csp_media.py')
    check(client_path.is_file(), 'build dist/csp-media before running the project contract tests')
    labels = {
        't2i': '文生图', 'i2i': '图生图', 't2v': '文生视频',
        'i2v': '图生视频', 'r2v': '参考生视频', 'voice': '声音克隆', 'speech': '语音合成',
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200, mime='application/json'):
            data = json.dumps(value, ensure_ascii=False).encode('utf-8') if mime == 'application/json' else value
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, location):
            self.send_response(302)
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def dispatch(self):
            body = None
            if self.command == 'POST':
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            auth = self.headers.get('Authorization')
            observed.append((self.command, self.path, body, auth))
            api = self.path.startswith('/v1/') or self.path == '/model/info'
            check(auth == ('Bearer ' + key) if api else auth is None, 'API/download authentication separation')

            if self.path == '/v1/models':
                self.reply({'data': [{'id': 'contract-all'}, {'id': 'xuanmap-voice'}, {'id': 'video-t2v-only'}]})
            elif self.path == '/model/info':
                self.reply({'data': [
                    {'model_name': 'contract-all', 'model_info': {'mode': 'video_generation', 'capabilities': labels}},
                    {'model_name': 'xuanmap-voice', 'model_info': {'mode': 'audio_voice_enrollment'}},
                    {'model_name': 'video-t2v-only', 'model_info': {'mode': 'video_generation', 'capabilities': {'t2v': '文生视频'}}},
                ]})
            elif self.path == '/v1/images/generations':
                prompt = body.get('prompt')
                if prompt == 'api-error':
                    self.reply({'error': {'message': key}}, 429)
                elif prompt == 'api-redirect':
                    self.redirect(origin + '/v1/images/generations/redirect-target')
                elif prompt == 'secret-response':
                    self.reply({'id': key})
                elif prompt == 'artifact-redirect':
                    self.reply({'data': [{'url': origin + '/artifact/redirect.png'}]})
                elif prompt == 'async-image':
                    self.reply({'id': image_id, 'status': 'queued'}, 202)
                else:
                    self.reply({'data': [{'url': origin + '/artifact/image.png'}]})
            elif self.path == '/v1/images/generations/' + image_id:
                self.reply({'id': image_id, 'status': 'completed', 'data': [
                    {'b64_json': base64.b64encode(image_bytes).decode('ascii')}
                ]})
            elif self.path == '/v1/videos' and self.command == 'POST':
                self.reply({'id': video_id, 'status': 'queued'}, 202)
            elif self.path == '/v1/videos/' + video_id:
                self.reply({'id': video_id, 'status': 'completed', 'usage': {'video_url': origin + '/artifact/video.mp4'}})
            elif self.path == '/v1/audio/voices':
                self.reply({'voice_id': 'local-authorized-voice'})
            elif self.path == '/v1/audio/speech':
                self.reply(speech_bytes, mime='audio/wav')
            elif self.path == '/artifact/image.png':
                self.reply(image_bytes, mime='image/png')
            elif self.path == '/artifact/video.mp4':
                self.reply(video_bytes, mime='video/mp4')
            elif self.path == '/artifact/redirect.png':
                self.redirect(origin + '/artifact/image.png')
            else:
                self.reply({'error': 'unexpected local route'}, 404)

        def handle_request(self):
            try:
                self.dispatch()
            except Exception:
                handler_errors.append('fake gateway assertion failed')
                try:
                    self.reply({'error': 'fake gateway assertion failed'}, 500)
                except OSError:
                    pass

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    origin = 'http://127.0.0.1:' + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    outputs = []
    invocations = 0
    try:
        with tempfile.TemporaryDirectory(prefix='csp-contract-') as temporary:
            output_dir = Path(temporary) / 'artifacts'
            env = os.environ.copy()
            env.update(CSP_API_KEY=key, CSP_BASE_URL=origin + '/v1', NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
            for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
                env.pop(name, None)

            def invoke(req=None, command='run', expected=0, missing_key=False):
                nonlocal invocations
                child_env = env.copy()
                if missing_key:
                    child_env.pop('CSP_API_KEY', None)
                args = [sys.executable, '-S', str(client_path), command]
                if req is not None:
                    args += ['--request', '-']
                completed = subprocess.run(args, input=json.dumps(req, ensure_ascii=False) if req is not None else '',
                                           capture_output=True, text=True, env=child_env, timeout=15)
                invocations += 1
                check(key not in completed.stdout and key not in completed.stderr, 'credential appeared in process output')
                check(completed.returncode == expected, 'unexpected client exit status: ' + completed.stdout)
                value = json.loads(completed.stdout)
                outputs.append(completed.stdout)
                return value

            def request(operation, **fields):
                value = {'operation': operation, 'model': 'contract-all', 'quantity': 1,
                         'output_dir': str(output_dir), 'poll_timeout': 2, 'poll_interval': 0.01}
                value.update(fields)
                return value

            def saved(value, expected_bytes):
                check(value['status'] == 'completed', 'completed result')
                check(len(value['saved_paths']) == 1, 'one saved file')
                path = Path(value['saved_paths'][0])
                check(path.is_absolute() and path.parent == output_dir.resolve(), 'actual output path')
                check(path.read_bytes() == expected_bytes, 'exact artifact bytes')

            catalog = invoke(command='models')
            check(catalog['discovery']['aliases'] == ['contract-all', 'xuanmap-voice', 'video-t2v-only'], 'gateway alias discovery')
            check(catalog['discovery']['model_info']['data'][0]['model_info']['capabilities']['t2v'] == '文生视频', 'document-shaped capability metadata')

            # Image operations: synchronous URL artifact, async base64 artifact, and i2i payload.
            image = invoke(request('i2i', prompt='sync-image', images=['https://public.example/reference.png']))
            saved(image, image_bytes)
            async_image = invoke(request('t2i', prompt='async-image'))
            saved(async_image, image_bytes)
            check(async_image['task_id'] == image_id, 'encoded image ID preserved')

            # All documented video modes; check fields observed by the fake gateway.
            t2v = invoke(request('t2v', prompt='text video', seconds=4, size='640x360'))
            saved(t2v, video_bytes)
            check(t2v['task_id'] == video_id, 'encoded video ID preserved')
            i2v = invoke(request('i2v', prompt='animate this frame', seconds=5, size='720x1280',
                                 img_url='https://public.example/first-frame.png'))
            saved(i2v, video_bytes)
            r2v_urls = invoke(request('r2v', prompt='use these references', seconds=6, size='720x1280',
                                      reference_urls=['https://public.example/reference.png']))
            saved(r2v_urls, video_bytes)
            r2v_media = invoke(request('r2v', prompt='use typed references', seconds=6, size='720x1280',
                                       media=[{'type': 'reference_image', 'url': 'https://public.example/reference.png'},
                                              {'type': 'reference_audio', 'url': 'https://public.example/reference.wav'}]))
            saved(r2v_media, video_bytes)

            # Enrollment is gated by consent; Xuanmap format confirmation comes from discovered alias metadata.
            voice = invoke(request('voice', model='contract-all',
                                   reference_audio='https://public.example/voice.wav', consent=True,
                                   name='authorized voice', prefix='demo', target_model='contract-all'))
            check(voice['voice_id'] == 'local-authorized-voice', 'voice_id returned')
            xuan_voice = invoke(request('voice', model='xuanmap-voice',
                                        reference_audio='https://public.example/xuan.wav', consent=True,
                                        public_pcm_wav_16k_mono=True))
            check(xuan_voice['voice_id'] == 'local-authorized-voice', 'Xuanmap enrollment accepted with format confirmation')
            speech = invoke(request('speech', text='本地语音验证', voice_id=voice['voice_id'], voice_authorized=True))
            saved(speech, speech_bytes)

            # Zero-timeout task is reported pending; poll resumes with the exact ID and no second POST.
            pending = invoke(request('t2v', prompt='resume video', seconds=4, size='640x360', poll_timeout=0), expected=7)
            check(pending['status'] == 'pending' and pending['task_id'] == video_id, 'pending original video ID')
            posts_before_resume = len([event for event in observed if event[0] == 'POST'])
            resumed = invoke({'operation': 'video', 'model': 'contract-all', 'task_id': pending['task_id'],
                              'output_dir': str(output_dir), 'poll_timeout': 2, 'poll_interval': 0.01}, command='poll')
            saved(resumed, video_bytes)
            check(len([event for event in observed if event[0] == 'POST']) == posts_before_resume, 'poll did not submit a new task')

            # Input and discovery boundaries must stop before media POST.
            posts_before_validation = len([event for event in observed if event[0] == 'POST'])
            for incomplete in (
                request('t2v', prompt='missing duration', size='640x360'),
                request('voice', reference_audio='https://public.example/voice.wav', consent=False),
                request('voice', model='xuanmap-voice', reference_audio='https://public.example/xuan.wav', consent=True),
                request('voice', reference_audio='https://public.example/voice.wav', consent=True, target_model='unknown-target'),
                request('i2v', prompt='private frame', seconds=4, size='640x360', img_url='http://127.0.0.1/private.png'),
                request('t2i', prompt='unavailable alias', model='unknown-alias'),
                request('i2v', prompt='unsupported capability', model='video-t2v-only', seconds=4,
                        size='640x360', img_url='https://public.example/first.png'),
            ):
                result = invoke(incomplete, expected=2)
                check(result['status'] == 'clarification' and result.get('missing'), 'boundary clarification')
            check(len([event for event in observed if event[0] == 'POST']) == posts_before_validation, 'clarifications made no media POST')

            # Missing key is local and distinct from an actual gateway HTTP failure.
            before_missing_key = len(observed)
            missing = invoke(request('t2i', prompt='no key'), expected=3, missing_key=True)
            check(missing['error']['code'] == 'missing_key' and len(observed) == before_missing_key, 'missing-key behavior')
            api_error = invoke(request('t2i', prompt='api-error'), expected=4)
            check(api_error['error']['code'] == 'api_error', 'API failure behavior')
            post_count = len([event for event in observed if event[0] == 'POST' and event[2] and event[2].get('prompt') == 'api-error'])
            check(post_count == 1, 'API failure was not retried')

            api_redirect = invoke(request('t2i', prompt='api-redirect'), expected=4)
            check(api_redirect['error']['code'] == 'api_error', 'API redirect rejected')
            download_redirect = invoke(request('t2i', prompt='artifact-redirect'), expected=6)
            check(download_redirect['error']['code'] == 'download_error' and not download_redirect['saved_paths'], 'artifact redirect rejected')
            leak = invoke(request('t2i', prompt='secret-response'), expected=6)
            check(leak['error']['code'] == 'credential_exposure', 'credential-bearing successful response rejected')
            before_secret_request = len(observed)
            secret_input = invoke(request('t2i', prompt=key), expected=6)
            check(secret_input['error']['code'] == 'credential_exposure' and len(observed) == before_secret_request, 'credential-bearing request blocked before network')

            posts = [event for event in observed if event[0] == 'POST']
            def payload_for(route, prompt):
                return next(event[2] for event in posts if event[1] == route and event[2].get('prompt') == prompt)

            image_payload = payload_for('/v1/images/generations', 'sync-image')
            check(image_payload['model'] == 'contract-all' and image_payload['n'] == 1 and
                  image_payload['extra_body']['images'] == ['https://public.example/reference.png'], 'image/i2i fields')
            text_video = payload_for('/v1/videos', 'text video')
            image_video = payload_for('/v1/videos', 'animate this frame')
            references = payload_for('/v1/videos', 'use these references')
            typed = payload_for('/v1/videos', 'use typed references')
            check(text_video['seconds'] == 4 and text_video['size'] == '640x360' and 'extra_body' not in text_video, 't2v payload')
            check(image_video['extra_body'] == {'img_url': 'https://public.example/first-frame.png'}, 'i2v payload')
            check(references['extra_body'] == {'gen_mode': 'r2v', 'reference_urls': ['https://public.example/reference.png']}, 'r2v URL payload')
            check(typed['extra_body']['gen_mode'] == 'r2v' and typed['extra_body']['media'] == [
                {'type': 'reference_image', 'url': 'https://public.example/reference.png'},
                {'type': 'reference_audio', 'url': 'https://public.example/reference.wav'}], 'typed r2v payload')
            voice_payloads = [event[2] for event in posts if event[1] == '/v1/audio/voices']
            check(all(p['media'][0]['type'] == 'reference_audio' for p in voice_payloads), 'voice enrollment typed media')
            check(voice_payloads[0]['name'] == 'authorized voice' and voice_payloads[0]['prefix'] == 'demo' and
                  voice_payloads[0]['target_model'] == 'contract-all', 'optional enrollment fields and alias')
            speech_payload = next(event[2] for event in posts if event[1] == '/v1/audio/speech')
            check(speech_payload['voice'] == 'local-authorized-voice' and speech_payload['input'] == '本地语音验证', 'speech fields')

            routes = {(event[0], event[1]) for event in observed}
            for route in [('GET', '/v1/models'), ('GET', '/model/info'), ('POST', '/v1/images/generations'),
                          ('GET', '/v1/images/generations/' + image_id), ('POST', '/v1/videos'),
                          ('GET', '/v1/videos/' + video_id), ('POST', '/v1/audio/voices'), ('POST', '/v1/audio/speech')]:
                check(route in routes, 'required observed gateway route')
            check(not handler_errors, 'fake gateway assertions')
            for path in Path(temporary).rglob('*'):
                if path.is_file():
                    check(key.encode() not in path.read_bytes(), 'credential appeared in saved file')
            check(all(key not in output for output in outputs), 'credential appeared in stdout')
            return {'status': 'passed', 'evidence': 'local_mock_only', 'client_invocations': invocations,
                    'checks': ['document_shaped_capability_discovery', 't2i_i2i', 't2v_i2v_r2v_urls_and_typed_media',
                               'async_image_video_poll_and_encoded_id', 'voice_consent_xuanmap_format_and_speech',
                               'clarification_without_submission', 'missing_key_and_api_error',
                               'api_and_download_redirect_rejection', 'credential_request_response_output_isolation',
                               'actual_saved_artifact_bytes_and_paths'], 'live_csp_verified': False}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    try:
        result = verify()
        code = 0
    except Exception:
        result = {'status': 'failed', 'evidence': 'local_mock_only',
                  'error': '本地合同断言或执行失败；未验证真实 CSP。', 'live_csp_verified': False}
        code = 1
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
