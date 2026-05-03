import os, re, uuid, threading, time, shutil, traceback
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
from sumy.utils import get_stop_words
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
try:
    from moviepy.editor import VideoFileClip, concatenate_videoclips
except ImportError:
    from moviepy import VideoFileClip, concatenate_videoclips

app = Flask(__name__)
CORS(app)
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tmp_jobs')
os.makedirs(TMP, exist_ok=True)
jobs = {}

NOISE_RE = re.compile(r'^\[.*\]$')
STOP_WORDS = get_stop_words('english')


def log(jid, msg):
    print(f'[{jid[:8]}] {msg}', flush=True)


def _video_id(url):
    for p in [r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})',
              r'(?:embed|shorts)/([A-Za-z0-9_-]{11})']:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise ValueError(f'Cannot parse video ID from: {url}')


def _get_transcript(vid):
    ytt = YouTubeTranscriptApi()
    try:
        transcript = ytt.fetch(vid, languages=['en', 'en-US', 'en-GB'])
    except NoTranscriptFound:
        transcript_list = ytt.list(vid)
        transcript = next(iter(transcript_list)).fetch()
    return [{'text': s.text, 'start': s.start, 'duration': s.duration} for s in transcript]


def _clean_entries(entries):
    return [e for e in entries if not NOISE_RE.match(e['text'].strip())]


def _group_into_topics(entries, window_size=8):
    """Split entries into overlapping windows of fixed size."""
    blocks = []
    for i in range(0, len(entries), max(1, window_size // 2)):
        chunk = entries[i:i + window_size]
        if chunk:
            blocks.append(chunk)
    return blocks


def _score_block(block):
    words = []
    for e in block:
        words += re.findall(r'\b[a-z]+\b', e['text'].lower())
    if not words:
        return 0.0
    content = [w for w in words if w not in STOP_WORDS and len(w) > 2]
    unique = len(set(content))
    dur = (block[-1]['start'] + block[-1]['duration']) - block[0]['start']
    return unique * min(dur, 30.0) / max(dur, 1.0)


def _block_span(block, padding=0.1):
    return max(0.0, block[0]['start'] - padding), block[-1]['start'] + block[-1]['duration'] + padding


def _merge_spans(spans, merge_gap=1.0):
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def _select_segments(entries, video_dur, target_ratio=0.60):
    """Keep best-scored windows until we reach target_ratio * video_dur."""
    blocks = _group_into_topics(entries)
    if not blocks:
        raise ValueError('No blocks found.')

    target = video_dur * target_ratio
    scored = sorted(blocks, key=_score_block, reverse=True)

    kept, total = [], 0.0
    for block in scored:
        if total >= target:
            break
        s, e = _block_span(block)
        kept.append(block)
        total += e - s

    # Merge overlapping/close spans from overlapping windows
    spans = [_block_span(b) for b in kept]
    spans = _merge_spans(spans, merge_gap=0.5)
    spans.sort()

    # If still over target after merge, trim from the end
    final, total = [], 0.0
    for s, e in spans:
        if total >= target:
            break
        dur = e - s
        if total + dur > target:
            e = s + (target - total)
        final.append((s, e))
        total += e - s

    return final


def _total_dur(segs):
    return sum(e - s for s, e in segs)


def _download_video(url, job_dir):
    opts = {
        'format': 'bestvideo[ext=mp4][protocol!=m3u8]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best',
        'outtmpl': os.path.join(job_dir, 'video.%(ext)s'),
        'quiet': False,
        'retries': 5,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36'},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    for ext in ('mp4', 'webm', 'mkv'):
        p = os.path.join(job_dir, f'video.{ext}')
        if os.path.exists(p):
            return p
    raise FileNotFoundError('Video download produced no output file.')


def _cut_and_encode(video_path, segs, out_path):
    clip = VideoFileClip(video_path)
    dur = clip.duration
    parts = []
    for s, e in segs:
        start = max(0.0, s)
        end = min(e, dur - 0.05)
        if end > start + 0.1:
            try:
                parts.append(clip.subclipped(start, end))
            except AttributeError:
                parts.append(clip.subclip(start, end))
    if not parts:
        raise ValueError('No valid segments to cut.')
    final = parts[0] if len(parts) == 1 else concatenate_videoclips(parts, method='compose')
    final.write_videofile(
        out_path, codec='libx264', audio_codec='aac',
        temp_audiofile=out_path + '.tmp.m4a', remove_temp=True, logger=None
    )
    clip.close()
    final.close()


def _run_job(job_id, url):
    log(job_id, f'Thread started: {url}')
    job_dir = os.path.join(TMP, job_id)
    os.makedirs(job_dir, exist_ok=True)

    def upd(status, stage=None, message=None, video_url=None):
        jobs[job_id]['status'] = status
        if stage is not None:
            jobs[job_id]['stage'] = stage
        jobs[job_id]['message'] = message
        if video_url is not None:
            jobs[job_id]['video_url'] = video_url
        log(job_id, f"status={status} stage={jobs[job_id]['stage']} msg={message}")

    try:
        vid = _video_id(url)
        log(job_id, f'Video ID: {vid}')

        upd('running', stage='download', message='Fetching transcript...')
        try:
            entries = _clean_entries(_get_transcript(vid))
            log(job_id, f'Transcript: {len(entries)} entries')
        except (TranscriptsDisabled, NoTranscriptFound, StopIteration) as e:
            upd('error', message=f'No captions available: {e}')
            return
        except Exception as e:
            upd('error', message=f'Transcript error: {e}')
            log(job_id, traceback.format_exc())
            return

        upd('running', stage='download', message='Downloading video...')
        try:
            video_path = _download_video(url, job_dir)
            log(job_id, f'Downloaded: {video_path}')
        except Exception as e:
            upd('error', message=f'Download failed: {e}')
            log(job_id, traceback.format_exc())
            return

        # Get actual video duration
        probe = VideoFileClip(video_path)
        video_dur = probe.duration
        probe.close()
        log(job_id, f'Video duration: {video_dur:.1f}s, target: {video_dur * 0.60:.1f}s')

        upd('running', stage='analyze', message='Selecting best 60% of content...')
        try:
            segments = _select_segments(entries, video_dur, target_ratio=0.60)
            total = _total_dur(segments)
            log(job_id, f'{len(segments)} segments, total={total:.1f}s ({100*total/video_dur:.0f}% of original)')
        except Exception as e:
            upd('error', message=f'Analysis failed: {e}')
            log(job_id, traceback.format_exc())
            return

        upd('running', stage='cut', message=f'Cutting {len(segments)} segments...')
        upd('running', stage='encode', message='Encoding final video...')
        out_path = os.path.join(job_dir, 'summary.mp4')
        try:
            _cut_and_encode(video_path, segments, out_path)
            log(job_id, f'Encoded: {out_path}')
        except Exception as e:
            upd('error', message=f'Encode failed: {e}')
            log(job_id, traceback.format_exc())
            return

        upd('done', video_url=f'/video/{job_id}/summary.mp4')

        def _cleanup():
            time.sleep(1800)
            shutil.rmtree(job_dir, ignore_errors=True)
            jobs.pop(job_id, None)
        threading.Thread(target=_cleanup, daemon=True).start()

    except Exception as e:
        log(job_id, traceback.format_exc())
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['message'] = str(e)


@app.route('/')
def index():
    return send_file('index.html')

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/process', methods=['POST'])
def process():
    data = request.get_json(silent=True) or {}
    url = data.get('video_url', '').strip()
    if not url:
        return jsonify(success=False, message='No URL provided.'), 400
    if not re.match(r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+', url):
        return jsonify(success=False, message='Invalid YouTube URL.'), 400
    job_id = str(uuid.uuid4())
    jobs[job_id] = {'status': 'queued', 'stage': None, 'video_url': None, 'message': None}
    t = threading.Thread(target=_run_job, args=(job_id, url), daemon=True)
    t.start()
    log(job_id, f'Queued, thread alive={t.is_alive()}')
    return jsonify(success=True, job_id=job_id)

@app.route('/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(status='error', message='Job not found.'), 404
    return jsonify(job)

@app.route('/jobs')
def list_jobs():
    return jsonify(jobs)

@app.route('/video/<job_id>/<filename>')
def serve_video(job_id, filename):
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '', filename)
    path = os.path.join(TMP, job_id, safe)
    if not os.path.exists(path):
        return jsonify(error='File not found.'), 404
    return send_file(path, mimetype='video/mp4', conditional=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 4000))
    print(f'SummaryScape on http://0.0.0.0:{port}', flush=True)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
