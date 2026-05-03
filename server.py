import os, re, uuid, threading, time, shutil, traceback, json
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

# =========================
# STORAGE SETUP
# =========================
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tmp_jobs')
os.makedirs(TMP, exist_ok=True)

jobs = {}
JOBS_FILE = "jobs.json"

def save_jobs():
    try:
        with open(JOBS_FILE, "w") as f:
            json.dump(jobs, f)
    except Exception as e:
        print("Save error:", e)

def load_jobs():
    global jobs
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE) as f:
                jobs = json.load(f)
        except:
            jobs = {}

load_jobs()

# =========================
# HELPERS
# =========================
NOISE_RE = re.compile(r'^\[.*\]$')
STOP_WORDS = get_stop_words('english')

def log(jid, msg):
    print(f'[{jid[:8]}] {msg}', flush=True)

@app.route('/debug')
def debug():
    return {"jobs": list(jobs.keys())}

def _video_id(url):
    for p in [r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})',
              r'(?:embed|shorts)/([A-Za-z0-9_-]{11})']:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise ValueError('Invalid YouTube URL')

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
    blocks = _group_into_topics(entries)
    target = video_dur * target_ratio
    scored = sorted(blocks, key=_score_block, reverse=True)

    spans = []
    total = 0.0
    for block in scored:
        if total >= target:
            break
        s, e = _block_span(block)
        spans.append((s, e))
        total += e - s

    spans = _merge_spans(spans, merge_gap=0.5)
    return spans

def _download_video(url, job_dir):
    opts = {
        'format': 'best[ext=mp4]',
        'outtmpl': os.path.join(job_dir, 'video.%(ext)s'),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    for ext in ('mp4', 'webm', 'mkv'):
        p = os.path.join(job_dir, f'video.{ext}')
        if os.path.exists(p):
            return p

    raise FileNotFoundError('No video found')

def _cut_and_encode(video_path, segs, out_path):
    clip = VideoFileClip(video_path)
    parts = []

    for s, e in segs:
        parts.append(clip.subclip(s, e))

    final = concatenate_videoclips(parts)
    final.write_videofile(out_path, codec='libx264', audio_codec='aac')

    clip.close()
    final.close()

# =========================
# JOB RUNNER
# =========================
def _run_job(job_id, url):
    job_dir = os.path.join(TMP, job_id)
    os.makedirs(job_dir, exist_ok=True)

    def upd(status, stage=None, message=None, video_url=None):
        jobs[job_id]['status'] = status
        if stage:
            jobs[job_id]['stage'] = stage
        jobs[job_id]['message'] = message
        if video_url:
            jobs[job_id]['video_url'] = video_url

        save_jobs()

    try:
        vid = _video_id(url)

        upd('running', 'transcript', 'Fetching transcript')
        entries = _clean_entries(_get_transcript(vid))

        upd('running', 'download', 'Downloading video')
        video_path = _download_video(url, job_dir)

        clip = VideoFileClip(video_path)
        segments = _select_segments(entries, clip.duration)
        clip.close()

        upd('running', 'encoding', 'Creating summary')
        out_path = os.path.join(job_dir, 'summary.mp4')
        _cut_and_encode(video_path, segments, out_path)

        upd('done', video_url=f'/video/{job_id}/summary.mp4')

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['message'] = str(e)
        save_jobs()

# =========================
# ROUTES
# =========================
@app.route('/')
def index():
    return send_file('index.html')

@app.route('/process', methods=['POST'])
def process():
    data = request.get_json()
    url = data.get('video_url', '')

    job_id = str(uuid.uuid4())

    jobs[job_id] = {
        'status': 'queued',
        'stage': None,
        'message': None,
        'video_url': None
    }
    save_jobs()

    threading.Thread(target=_run_job, args=(job_id, url), daemon=True).start()

    return jsonify(success=True, job_id=job_id)

@app.route('/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(status='error', message='Job not found'), 404
    return jsonify(job)

@app.route('/video/<job_id>/<filename>')
def serve_video(job_id, filename):
    path = os.path.join(TMP, job_id, filename)
    if not os.path.exists(path):
        return jsonify(error='File not found'), 404
    return send_file(path)

# =========================
# RUN
# =========================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 4000))
    app.run(host='0.0.0.0', port=port)