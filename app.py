from flask import Flask, request, jsonify
from flask_cors import CORS
import random
import string
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import csv
import io
import os
import math
import sqlite3
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), 'attendance.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row 
    conn.execute("PRAGMA journal_mode=WAL")  
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                code         TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                venue        TEXT NOT NULL,
                gmail        TEXT NOT NULL,
                organized_by TEXT NOT NULL,
                lat          REAL NOT NULL,
                lng          REAL NOT NULL,
                radius_m     INTEGER NOT NULL DEFAULT 100,
                active       INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attendance (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_code  TEXT NOT NULL REFERENCES events(code),
                name        TEXT NOT NULL,
                dept        TEXT NOT NULL,
                section     TEXT NOT NULL,
                review      TEXT,
                lat         REAL,
                lng         REAL,
                distance_m  INTEGER,
                ip          TEXT NOT NULL,
                timestamp   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_attendance_event
                ON attendance(event_code);

            CREATE INDEX IF NOT EXISTS idx_attendance_ip
                ON attendance(event_code, ip);
        """)

init_db()



def generate_code():
    with get_db() as conn:
        while True:
            code = ''.join(random.choices(string.digits, k=6))
            exists = conn.execute(
                "SELECT 1 FROM events WHERE code = ?", (code,)
            ).fetchone()
            if not exists:
                return code

def haversine_meters(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi       = math.radians(lat2 - lat1)
    dlambda    = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def row_to_dict(row):
    return dict(row) if row else None



@app.route('/api/create-event', methods=['POST'])
def create_event():
    data         = request.json
    name         = data.get('name', '').strip()
    venue        = data.get('venue', '').strip()
    gmail        = data.get('gmail', '').strip()
    organized_by = data.get('organized_by', '').strip()
    event_lat    = data.get('lat')
    event_lng    = data.get('lng')
    radius_m     = int(data.get('radius_m', 100))

    if not all([name, venue, gmail, organized_by]):
        return jsonify({'error': 'All fields are required'}), 400
    if event_lat is None or event_lng is None:
        return jsonify({'error': 'Event GPS location is required'}), 400

    code = generate_code()
    now  = datetime.utcnow().isoformat()

    with get_db() as conn:
        conn.execute("""
            INSERT INTO events
              (code, name, venue, gmail, organized_by, lat, lng, radius_m, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (code, name, venue, gmail, organized_by,
              float(event_lat), float(event_lng), radius_m, now))

    return jsonify({
        'code': code,
        'event': {
            'code': code, 'name': name, 'venue': venue,
            'gmail': gmail, 'organized_by': organized_by,
            'radius_m': radius_m, 'active': True, 'created_at': now
        }
    })


@app.route('/api/event/<code>', methods=['GET'])
def get_event(code):
    with get_db() as conn:
        event = row_to_dict(
            conn.execute("SELECT * FROM events WHERE code = ?", (code,)).fetchone()
        )
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        count = conn.execute(
            "SELECT COUNT(*) FROM attendance WHERE event_code = ?", (code,)
        ).fetchone()[0]

    safe = {k: v for k, v in event.items() if k not in ('lat', 'lng')}
    safe['active'] = bool(safe['active'])
    return jsonify({'event': safe, 'count': count})


@app.route('/api/attend', methods=['POST'])
def mark_attendance():
    data    = request.json
    code    = data.get('code', '').strip()
    name    = data.get('name', '').strip()
    dept    = data.get('dept', '').strip()
    section = data.get('section', '').strip()
    review  = data.get('review', '').strip()
    s_lat   = data.get('lat')
    s_lng   = data.get('lng')
    ip      = request.remote_addr

    if not all([code, name, dept, section]):
        return jsonify({'error': 'Name, department, and section are required'}), 400

    with get_db() as conn:
        event = row_to_dict(
            conn.execute("SELECT * FROM events WHERE code = ?", (code,)).fetchone()
        )
        if not event:
            return jsonify({'error': 'Invalid event code'}), 404
        if not event['active']:
            return jsonify({'error': 'This event has ended'}), 403

        # GPS required
        if s_lat is None or s_lng is None:
            return jsonify({'error': 'Location access is required to mark attendance. Enable GPS and try again.'}), 403


        distance_m = haversine_meters(
            event['lat'], event['lng'],
            float(s_lat), float(s_lng)
        )
        if distance_m > event['radius_m']:
            return jsonify({
                'error': (f"You are {int(distance_m)} m away from the venue. "
                          f"You must be within {event['radius_m']} m to mark attendance."),
                'distance_m': int(distance_m),
                'allowed_m':  event['radius_m']
            }), 403

  
        dup = conn.execute(
            "SELECT 1 FROM attendance WHERE event_code = ? AND ip = ?", (code, ip)
        ).fetchone()
        if dup:
            return jsonify({'error': 'Attendance already marked from this device or network.'}), 409


        conn.execute("""
            INSERT INTO attendance
              (event_code, name, dept, section, review, lat, lng, distance_m, ip, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (code, name, dept, section, review,
              float(s_lat), float(s_lng), int(distance_m),
              ip, datetime.utcnow().isoformat()))

        count = conn.execute(
            "SELECT COUNT(*) FROM attendance WHERE event_code = ?", (code,)
        ).fetchone()[0]

    return jsonify({
        'message':    'Attendance marked successfully',
        'distance_m': int(distance_m),
        'count':      count
    })


@app.route('/api/stop-event', methods=['POST'])
def stop_event():
    data = request.json
    code = data.get('code', '').strip()
    with get_db() as conn:
        result = conn.execute(
            "UPDATE events SET active = 0 WHERE code = ?", (code,)
        )
        if result.rowcount == 0:
            return jsonify({'error': 'Event not found'}), 404
        count = conn.execute(
            "SELECT COUNT(*) FROM attendance WHERE event_code = ?", (code,)
        ).fetchone()[0]
    return jsonify({'message': 'Event stopped', 'count': count})


@app.route('/api/attendance/<code>', methods=['GET'])
def get_attendance(code):
    with get_db() as conn:
        event = conn.execute(
            "SELECT 1 FROM events WHERE code = ?", (code,)
        ).fetchone()
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        rows = conn.execute(
            "SELECT * FROM attendance WHERE event_code = ? ORDER BY id ASC", (code,)
        ).fetchall()
    records = [dict(r) for r in rows]
    return jsonify({'records': records, 'count': len(records)})


@app.route('/api/export/<code>', methods=['POST'])
def export_attendance(code):
    with get_db() as conn:
        event = row_to_dict(
            conn.execute("SELECT * FROM events WHERE code = ?", (code,)).fetchone()
        )
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        rows = conn.execute(
            "SELECT * FROM attendance WHERE event_code = ? ORDER BY id ASC", (code,)
        ).fetchall()

    records = [dict(r) for r in rows]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['#', 'Name', 'Department', 'Section', 'Review',
                     'Latitude', 'Longitude', 'Distance from Venue (m)',
                     'IP Address', 'Timestamp'])
    for i, r in enumerate(records, 1):
        writer.writerow([
            i, r['name'], r['dept'], r['section'], r.get('review', ''),
            r.get('lat', ''), r.get('lng', ''), r.get('distance_m', ''),
            r['ip'], r['timestamp']
        ])
    csv_content = output.getvalue()

    gmail_user = os.environ.get('GMAIL_USER', '')
    gmail_pass = os.environ.get('GMAIL_PASS', '')

    if not gmail_user or not gmail_pass:
        return jsonify({
            'message': 'Email not configured — CSV returned for local download.',
            'csv':     csv_content,
            'count':   len(records)
        })

    try:
        msg = MIMEMultipart()
        msg['From']    = gmail_user
        msg['To']      = event['gmail']
        msg['Subject'] = f"Attendance Report – {event['name']}"
        body = (
            f"Hi {event['organized_by']},\n\n"
            f"Attached is the attendance report for \"{event['name']}\" at {event['venue']}.\n"
            f"Total verified attendees: {len(records)}\n"
            f"Geofence radius: {event['radius_m']} m\n\n"
            "— AttendanceIQ"
        )
        msg.attach(MIMEText(body, 'plain'))
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(csv_content.encode())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition',
                        f'attachment; filename="attendance_{code}.csv"')
        msg.attach(part)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, event['gmail'], msg.as_string())

        return jsonify({'message': f'Report emailed to {event["gmail"]}', 'count': len(records)})
    except Exception as e:
        return jsonify({'error': f'Email failed: {e}', 'csv': csv_content, 'count': len(records)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
