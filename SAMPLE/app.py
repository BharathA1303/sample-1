from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    send_file,
)
from dotenv import load_dotenv
import json
import os
import tempfile
load_dotenv()
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
import uuid
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
from io import BytesIO, StringIO
import sqlite3
import csv


app = Flask(__name__)
app.secret_key = "your-secret-key-here-change-this-in-production"

# Configuration - Enhanced file types
ALLOWED_EXTENSIONS = {
    "pdf", "pptx", "docx", "txt", "doc",
    "jpg", "jpeg", "png", "gif", "bmp", "svg", "webp",
    "mp4", "mov", "avi", "mkv", "webm", "flv",
    "zip", "rar", "7z", "tar", "gz",
}

app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

# S3 Configuration
S3_BUCKET = "notesdk"
S3_REGION = "ap-south-1"
USERS_DB_KEY = "users/users.db"

# Create temp directory if it doesn't exist
import tempfile
TEMP_DIR = tempfile.gettempdir()
TEMP_DB_PATH = os.path.join(TEMP_DIR, "notes_dock_users.db")

# Initialize S3 client
try:
    S3_CLIENT = boto3.client("s3", region_name=S3_REGION)
    print(f"✅ S3 initialized: Bucket={S3_BUCKET}, Region={S3_REGION}")
    S3_CLIENT.head_bucket(Bucket=S3_BUCKET)
    print(f"✅ S3 bucket '{S3_BUCKET}' is accessible")
except ClientError as e:
    print(f"❌ S3 initialization failed: {e}")
    S3_CLIENT = None
except Exception as e:
    print(f"❌ Unexpected error during S3 initialization: {e}")
    S3_CLIENT = None

# Admin credentials
ADMIN_CREDENTIALS = {
    "year_1": {
        "username": os.getenv("ADMIN_1_USERNAME"),
        "password": generate_password_hash(os.getenv("ADMIN_1_PASSWORD"))
    },
    "year_2": {
        "username": os.getenv("ADMIN_2_USERNAME"),
        "password": generate_password_hash(os.getenv("ADMIN_2_PASSWORD"))
    },
    "year_3": {
        "username": os.getenv("ADMIN_3_USERNAME"),
        "password": generate_password_hash(os.getenv("ADMIN_3_PASSWORD"))
    },
}

# ==================== USER DATABASE FUNCTIONS ====================

def init_db():
    """Initialize SQLite database schema"""
    try:
        # Ensure directory exists
        db_dir = os.path.dirname(TEMP_DB_PATH)
        os.makedirs(db_dir, exist_ok=True)
        
        conn = sqlite3.connect(TEMP_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                department TEXT NOT NULL,
                year INTEGER NOT NULL,
                section TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                count INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(department, year, section, name, email)
            )
        ''')
        
        conn.commit()
        conn.close()
        print(f"✅ Database initialized at: {TEMP_DB_PATH}")
    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        raise

def download_db_from_s3():
    """Download database from S3 to local temp file"""
    if not S3_CLIENT:
        raise Exception("S3 client not initialized")
    
    try:
        S3_CLIENT.download_file(S3_BUCKET, USERS_DB_KEY, TEMP_DB_PATH)
        print(f"✅ Downloaded database from S3")
        return True
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "404" or error_code == "NoSuchKey":
            print(f"ℹ️  Database not found in S3, creating new")
            init_db()
            return False
        print(f"❌ S3 download failed: {e}")
        raise Exception(f"Failed to download database from S3: {str(e)}")

def upload_db_to_s3():
    """Upload database to S3"""
    if not S3_CLIENT:
        raise Exception("S3 client not initialized")
    
    try:
        S3_CLIENT.upload_file(TEMP_DB_PATH, S3_BUCKET, USERS_DB_KEY)
        print(f"✅ Uploaded database to S3")
        return True
    except ClientError as e:
        print(f"❌ S3 upload failed: {e}")
        raise Exception(f"Failed to upload database to S3: {str(e)}")

def get_db_connection():
    """Get database connection, ensuring latest version is downloaded"""
    try:
        # Ensure directory exists
        db_dir = os.path.dirname(TEMP_DB_PATH)
        os.makedirs(db_dir, exist_ok=True)
        
        # Try to download from S3
        try:
            download_db_from_s3()
        except Exception as e:
            print(f"Could not download from S3, creating new: {e}")
            # If download fails, create new db
            if not os.path.exists(TEMP_DB_PATH):
                init_db()
    
        conn = sqlite3.connect(TEMP_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        print(f"❌ Error connecting to database: {e}")
        raise Exception(f"Database connection error: {str(e)}")

def add_or_update_user(department, year, section, name, email):
    """Add new user or update count if exists"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if user exists
        cursor.execute('''
            SELECT * FROM users 
            WHERE LOWER(department) = LOWER(?)
            AND year = ?
            AND UPPER(section) = UPPER(?)
            AND LOWER(name) = LOWER(?)
            AND LOWER(email) = LOWER(?)
        ''', (department, int(year), section, name, email))
        
        existing_user = cursor.fetchone()
        
        if existing_user:
            # User exists, increment count
            cursor.execute('''
                UPDATE users 
                SET count = count + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (existing_user['id'],))
            print(f"✅ Updated user visit count: {name}")
            result = {'created': False, 'message': 'User updated'}
        else:
            # User doesn't exist, create new entry
            cursor.execute('''
                INSERT INTO users (department, year, section, name, email, count)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (department, int(year), section.upper(), name, email))
            print(f"✅ Added new user: {name}")
            result = {'created': True, 'message': 'New user created'}
        
        conn.commit()
        conn.close()
        
        # Upload updated database to S3
        upload_db_to_s3()
        
        return result
    except sqlite3.IntegrityError as e:
        print(f"Database integrity error: {e}")
        return {'created': False, 'message': 'User update failed'}
    except Exception as e:
        print(f"Error adding/updating user: {e}")
        raise Exception(f"Failed to process user: {str(e)}")

def get_all_users_sorted():
    """Get all users sorted by year and section"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Query sorted by year ASC, section ASC (A before B)
        cursor.execute('''
            SELECT id, department, year, section, name, email, count, 
                   created_at, updated_at
            FROM users
            ORDER BY year ASC, section ASC, name ASC
        ''')
        
        users = cursor.fetchall()
        conn.close()
        
        return users
    except Exception as e:
        print(f"Error fetching users: {e}")
        return []

def export_users_to_csv():
    """Export users database to CSV format"""
    try:
        users = get_all_users_sorted()
        
        output = StringIO()
        fieldnames = ['S.no', 'Department', 'Year', 'Section', 'Name', 'Email', 'Count']
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        
        writer.writeheader()
        for idx, user in enumerate(users, 1):
            writer.writerow({
                'S.no': idx,
                'Department': user['department'],
                'Year': user['year'],
                'Section': user['section'],
                'Name': user['name'],
                'Email': user['email'],
                'Count': user['count']
            })
        
        return output.getvalue()
    except Exception as e:
        print(f"Error exporting users: {e}")
        return None

# ==================== EXISTING FUNCTIONS ====================

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def get_s3_key(year, semester, filename):
    return f"year_{year}/{semester}sem/{filename}"

def s3_upload_fileobj(file_obj, bucket, key):
    if not S3_CLIENT:
        raise Exception("S3 client not initialized")
    try:
        S3_CLIENT.upload_fileobj(file_obj, bucket, key)
        print(f"✅ Uploaded to S3: {key}")
        return True
    except ClientError as e:
        print(f"❌ S3 upload failed: {e}")
        raise Exception(f"Failed to upload file to S3: {str(e)}")

def s3_download_fileobj(bucket, key):
    if not S3_CLIENT:
        raise Exception("S3 client not initialized")
    try:
        file_obj = BytesIO()
        S3_CLIENT.download_fileobj(bucket, key, file_obj)
        file_obj.seek(0)
        print(f"✅ Downloaded from S3: {key}")
        return file_obj
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "404" or error_code == "NoSuchKey":
            print(f"⚠️  File not found in S3: {key}")
            return None
        print(f"❌ S3 download failed: {e}")
        raise Exception(f"Failed to download file from S3: {str(e)}")

def s3_delete_file(bucket, key):
    if not S3_CLIENT:
        raise Exception("S3 client not initialized")
    try:
        S3_CLIENT.delete_object(Bucket=bucket, Key=key)
        print(f"✅ Deleted from S3: {key}")
        return True
    except ClientError as e:
        print(f"❌ S3 delete failed: {e}")
        return False

def s3_upload_json(bucket, key, data):
    if not S3_CLIENT:
        raise Exception("S3 client not initialized")
    try:
        json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        S3_CLIENT.put_object(
            Bucket=bucket, Key=key, Body=json_bytes, ContentType="application/json"
        )
        print(f"✅ Uploaded JSON to S3: {key}")
        return True
    except ClientError as e:
        print(f"❌ S3 JSON upload failed: {e}")
        raise Exception(f"Failed to upload JSON to S3: {str(e)}")

def s3_download_json(bucket, key):
    if not S3_CLIENT:
        raise Exception("S3 client not initialized")
    try:
        response = S3_CLIENT.get_object(Bucket=bucket, Key=key)
        data = json.loads(response["Body"].read().decode("utf-8"))
        print(f"✅ Downloaded JSON from S3: {key}")
        return data
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "404" or error_code == "NoSuchKey":
            print(f"ℹ️  JSON file not found in S3, creating new: {key}")
            return None
        print(f"❌ S3 JSON download failed: {e}")
        raise Exception(f"Failed to download JSON from S3: {str(e)}")

def load_data(year, semester):
    if not S3_CLIENT:
        raise Exception("S3 is not available. Cannot load data.")

    data_key = get_s3_key(year, semester, "data.json")

    try:
        data = s3_download_json(S3_BUCKET, data_key)
        if data:
            return data
    except Exception as e:
        print(f"Error loading data: {e}")

    default_data = {
        "subjects": [],
        "stats": {
            "total_subjects": 0,
            "total_files": 0,
            "total_visits": 0,
            "total_downloads": 0,
            "storage_used": "0 MB",
            "last_updated": datetime.now().isoformat(),
        },
    }

    try:
        save_data(year, semester, default_data)
    except Exception as e:
        print(f"Warning: Could not create default data file: {e}")

    return default_data

def save_data(year, semester, data):
    if not S3_CLIENT:
        raise Exception("S3 is not available. Cannot save data.")

    if "stats" in data:
        data["stats"]["last_updated"] = datetime.now().isoformat()
        data["stats"]["total_subjects"] = len(data.get("subjects", []))
        total_files = sum(
            len(subject.get("units", [])) for subject in data.get("subjects", [])
        )
        data["stats"]["total_files"] = total_files

    data_key = get_s3_key(year, semester, "data.json")
    s3_upload_json(S3_BUCKET, data_key, data)

# ==================== ROUTES ====================

@app.route("/test-db")
def test_db():
    """Test database connection"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM users")
        result = cursor.fetchone()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'Database is working! Total users: {result["count"]}',
            'db_path': TEMP_DB_PATH
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Database error: {str(e)}',
            'db_path': TEMP_DB_PATH
        }), 500

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/api/register-user", methods=["POST"])
def register_user():
    """Register or update user in database"""
    try:
        data = request.json
        
        required_fields = ['department', 'year', 'section', 'name', 'email']
        for field in required_fields:
            if not data.get(field):
                return jsonify({
                    'success': False,
                    'message': f'Missing required field: {field}'
                }), 400
        
        department = data.get('department')
        year = data.get('year')
        section = data.get('section')
        name = data.get('name')
        email = data.get('email')
        
        result = add_or_update_user(department, year, section, name, email)
        
        session['department'] = department
        session['year'] = year
        session['section'] = section
        session['name'] = name
        session['email'] = email
        
        return jsonify({
            'success': True,
            'message': result['message']
        })
    except Exception as e:
        print(f"Error registering user: {e}")
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}'
        }), 500

@app.route("/api/export-users-csv")
def export_users_csv():
    """Export users database as CSV file"""
    try:
        if not session.get("admin_logged_in"):
            return jsonify({
                'success': False,
                'message': 'Not authorized'
            }), 403
        
        csv_content = export_users_to_csv()
        
        if csv_content:
            from flask import make_response
            response = make_response(csv_content)
            response.headers["Content-Disposition"] = "attachment; filename=users_export.csv"
            response.headers["Content-Type"] = "text/csv"
            return response
        else:
            return jsonify({
                'success': False,
                'message': 'Failed to export users'
            }), 500
    except Exception as e:
        print(f"Error exporting users: {e}")
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}'
        }), 500

@app.route("/api/get-users")
def get_users():
    """Get all users sorted by year and section (for admin dashboard)"""
    try:
        if not session.get("admin_logged_in"):
            return jsonify({
                'success': False,
                'message': 'Not authorized'
            }), 403
        
        users = get_all_users_sorted()
        users_list = [dict(user) for user in users]
        
        # Add S.no
        for idx, user in enumerate(users_list, 1):
            user['S.no'] = idx
        
        return jsonify({
            'success': True,
            'users': users_list
        })
    except Exception as e:
        print(f"Error fetching users: {e}")
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}'
        }), 500

@app.route("/subjects")
def subjects():
    department = request.args.get("department")
    year = request.args.get("year")
    semester = request.args.get("semester")
    name = request.args.get("name")
    email = request.args.get("email")
    section = request.args.get("section")

    if not all([department, year, semester]):
        return redirect(url_for("index"))

    session["department"] = department
    session["year"] = year
    session["semester"] = semester
    session["name"] = name
    session["email"] = email
    session["section"] = section

    try:
        data = load_data(year, semester)
        data["stats"]["total_visits"] = data["stats"].get("total_visits", 0) + 1
        save_data(year, semester, data)
    except Exception as e:
        print(f"Error loading subjects: {e}")
        return f"Error loading data: {str(e)}", 500

    return render_template(
        "subject.html",
        subjects=data["subjects"],
        department=department,
        year=year,
        semester=semester,
        name=name,
        email=email,
        section=section,
    )

@app.route("/admin/login", methods=["POST"])
def admin_login():
    username = request.json.get("username")
    password = request.json.get("password")

    year = session.get("year")
    if not year:
        return jsonify({"success": False, "message": "Please select year first"})

    admin_key = f"year_{year}"
    if admin_key in ADMIN_CREDENTIALS:
        admin_creds = ADMIN_CREDENTIALS[admin_key]
        if username == admin_creds["username"] and check_password_hash(
            admin_creds["password"], password
        ):
            session["admin_logged_in"] = True
            session["admin_year"] = year
            return jsonify({"success": True})

    return jsonify({"success": False, "message": "Invalid credentials"})

@app.route("/admin")
def admin_panel():
    if not session.get("admin_logged_in"):
        return redirect(url_for("subjects"))

    year = session.get("admin_year") or session.get("year")
    semester = session.get("semester")

    if not all([year, semester]):
        return redirect(url_for("index"))

    try:
        data = load_data(year, semester)
    except Exception as e:
        print(f"Error loading admin panel: {e}")
        return f"Error loading data: {str(e)}", 500

    return render_template(
        "admin.html",
        subjects=data["subjects"],
        stats=data["stats"],
        year=year,
        semester=semester,
    )

@app.route("/admin/add_subject", methods=["POST"])
def add_subject():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Not authorized"})

    year = session.get("admin_year") or session.get("year")
    semester = session.get("semester")

    subject_name = request.json.get("subject_name")
    subject_icon = request.json.get("subject_icon", "fas fa-book")

    if not subject_name:
        return jsonify({"success": False, "message": "Subject name is required"})

    try:
        data = load_data(year, semester)

        if any(s["name"].lower() == subject_name.lower() for s in data["subjects"]):
            return jsonify({"success": False, "message": "Subject already exists"})

        new_subject = {
            "id": str(uuid.uuid4()),
            "name": subject_name,
            "icon": subject_icon,
            "units": [],
            "created_at": datetime.now().isoformat(),
        }

        data["subjects"].append(new_subject)
        save_data(year, semester, data)

        return jsonify({"success": True, "subject": new_subject})
    except Exception as e:
        print(f"Error adding subject: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"})

@app.route("/admin/edit_subject", methods=["POST"])
def edit_subject():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Not authorized"})

    year = session.get("admin_year") or session.get("year")
    semester = session.get("semester")

    subject_id = request.json.get("subject_id")
    subject_name = request.json.get("subject_name")
    subject_icon = request.json.get("subject_icon")

    if not all([subject_id, subject_name, subject_icon]):
        return jsonify({"success": False, "message": "Missing required fields"})

    try:
        data = load_data(year, semester)

        for subject in data["subjects"]:
            if subject["id"] == subject_id:
                subject["name"] = subject_name
                subject["icon"] = subject_icon
                save_data(year, semester, data)
                return jsonify({"success": True, "subject": subject})

        return jsonify({"success": False, "message": "Subject not found"})
    except Exception as e:
        print(f"Error editing subject: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"})

@app.route("/admin/add_unit", methods=["POST"])
def add_unit():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Not authorized"})

    year = session.get("admin_year") or session.get("year")
    semester = session.get("semester")

    subject_id = request.form.get("subject_id")
    unit_number_str = request.form.get("unit_number", "")
    unit_title = request.form.get("unit_title")
    unit_description = request.form.get("unit_description", "")
    topics = request.form.get("topics", "")
    pages_count_str = request.form.get("pages_count", "0")

    if not all([subject_id, unit_title]):
        return jsonify({"success": False, "message": "Missing required fields"})

    try:
        unit_number = int(unit_number_str) if unit_number_str else 1
        pages_count = int(pages_count_str) if pages_count_str else 0
    except ValueError:
        return jsonify({"success": False, "message": "Invalid numeric values"})

    uploaded_file = request.files.get("file")
    filename = None

    if uploaded_file and uploaded_file.filename and allowed_file(uploaded_file.filename):
        try:
            filename = secure_filename(uploaded_file.filename)
            key = get_s3_key(year, semester, filename)
            file_stream = uploaded_file.stream
            s3_upload_fileobj(file_stream, S3_BUCKET, key)
        except Exception as e:
            print(f"Error uploading file: {e}")
            return jsonify(
                {"success": False, "message": f"File upload failed: {str(e)}"}
            )

    try:
        data = load_data(year, semester)

        subject_found = False
        for subject in data["subjects"]:
            if subject["id"] == subject_id:
                subject_found = True
                new_unit = {
                    "id": str(uuid.uuid4()),
                    "number": unit_number,
                    "title": unit_title,
                    "description": unit_description,
                    "topics": topics,
                    "pages_count": pages_count,
                    "filename": filename,
                    "icon": "fas fa-file-alt",
                    "created_at": datetime.now().isoformat(),
                }
                subject["units"].append(new_unit)
                break

        if not subject_found:
            return jsonify({"success": False, "message": "Subject not found"})

        save_data(year, semester, data)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error adding unit: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"})

@app.route("/admin/edit_unit", methods=["POST"])
def edit_unit():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Not authorized"})

    year = session.get("admin_year") or session.get("year")
    semester = session.get("semester")

    subject_id = request.form.get("subject_id")
    unit_id = request.form.get("unit_id")
    unit_number_str = request.form.get("unit_number")
    unit_title = request.form.get("unit_title")
    unit_description = request.form.get("unit_description", "")
    topics = request.form.get("topics", "")
    pages_count_str = request.form.get("pages_count", "0")

    if not all([subject_id, unit_id, unit_number_str, unit_title]):
        return jsonify({"success": False, "message": "Missing required fields"})

    try:
        unit_number = int(unit_number_str)
        pages_count = int(pages_count_str) if pages_count_str else 0
    except ValueError:
        return jsonify({"success": False, "message": "Invalid numeric values"})

    try:
        data = load_data(year, semester)

        unit_found = None
        for subject in data["subjects"]:
            if subject["id"] == subject_id:
                for unit in subject["units"]:
                    if unit["id"] == unit_id:
                        unit_found = unit

                        unit["number"] = unit_number
                        unit["title"] = unit_title
                        unit["description"] = unit_description
                        unit["topics"] = topics
                        unit["pages_count"] = pages_count

                        uploaded_file = request.files.get("file")
                        if uploaded_file and uploaded_file.filename and allowed_file(uploaded_file.filename):
                            if unit.get("filename"):
                                old_key = get_s3_key(year, semester, unit["filename"])
                                s3_delete_file(S3_BUCKET, old_key)

                            filename = secure_filename(uploaded_file.filename)
                            key = get_s3_key(year, semester, filename)
                            file_stream = uploaded_file.stream
                            s3_upload_fileobj(file_stream, S3_BUCKET, key)
                            unit["filename"] = filename

                        break
                if unit_found:
                    break

        if not unit_found:
            return jsonify({"success": False, "message": "Unit not found"})

        save_data(year, semester, data)
        return jsonify({"success": True, "unit": unit_found})
    except Exception as e:
        print(f"Error editing unit: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"})

@app.route("/admin/delete_unit", methods=["DELETE"])
def delete_unit():
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Not authorized"})

    year = session.get("admin_year") or session.get("year")
    semester = session.get("semester")

    subject_id = request.json.get("subject_id")
    unit_id = request.json.get("unit_id")

    if not all([subject_id, unit_id]):
        return jsonify({"success": False, "message": "Missing required fields"})

    try:
        data = load_data(year, semester)

        unit_deleted = False
        for subject in data["subjects"]:
            if subject["id"] == subject_id:
                for i, unit in enumerate(subject["units"]):
                    if unit["id"] == unit_id:
                        deleted_unit = subject["units"].pop(i)
                        unit_deleted = True

                        if deleted_unit.get("filename"):
                            key = get_s3_key(year, semester, deleted_unit["filename"])
                            s3_delete_file(S3_BUCKET, key)

                        break
                if unit_deleted:
                    break

        if not unit_deleted:
            return jsonify({"success": False, "message": "Unit not found"})

        save_data(year, semester, data)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error deleting unit: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"})

@app.route("/download/<filename>")
def download_file(filename):
    year = session.get("year")
    semester = session.get("semester")

    if not all([year, semester]):
        return "Invalid session", 400

    try:
        key = get_s3_key(year, semester, filename)
        file_obj = s3_download_fileobj(S3_BUCKET, key)

        if not file_obj:
            return "File not found", 404

        data = load_data(year, semester)
        data["stats"]["total_downloads"] = data["stats"].get("total_downloads", 0) + 1
        save_data(year, semester, data)

        return send_file(file_obj, as_attachment=True, download_name=filename)
    except Exception as e:
        print(f"Error downloading file: {e}")
        return f"Error downloading file: {str(e)}", 500

@app.route("/admin/delete_subject/<subject_id>", methods=["DELETE"])
def delete_subject(subject_id):
    if not session.get("admin_logged_in"):
        return jsonify({"success": False, "message": "Not authorized"})

    year = session.get("admin_year") or session.get("year")
    semester = session.get("semester")

    if not subject_id:
        return jsonify({"success": False, "message": "Subject ID is required"})

    try:
        data = load_data(year, semester)

        subject_to_remove = None
        for i, subject in enumerate(data["subjects"]):
            if subject["id"] == subject_id:
                subject_to_remove = data["subjects"].pop(i)
                break

        if not subject_to_remove:
            return jsonify({"success": False, "message": "Subject not found"})

        for unit in subject_to_remove.get("units", []):
            if unit.get("filename"):
                key = get_s3_key(year, semester, unit["filename"])
                s3_delete_file(S3_BUCKET, key)

        save_data(year, semester, data)
        return jsonify({"success": True})
    except Exception as e:
        print(f"Error deleting subject: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"})

# @app.route("/admin/logout")
# def admin_logout():
#     session.pop("admin_logged_in", None)
#     session.pop("admin_year", None)
#     return redirect(url_for("subjects"))

# @app.route("/admin/logout")
# def admin_logout():
#     session.pop("admin_logged_in", None)
#     session.pop("admin_year", None)

#     # Rebuild /subjects URL using existing session values
#     if "department" in session and "year" in session and "semester" in session:
#         return redirect(
#             url_for(
#                 "subjects",
#                 department=session["department"],
#                 year=session["year"],
#                 semester=session["semester"],
#                 name=session.get("name"),
#                 email=session.get("email"),
#                 section=session.get("section"),
#             )
#         )
    
#     return redirect(url_for("index"))

@app.route("/admin/logout")
def admin_logout():
    print("🔓 Admin logout initiated")
    
    session.pop("admin_logged_in", None)
    session.pop("admin_year", None)

    # Rebuild /subjects URL using existing session values
    if "department" in session and "year" in session and "semester" in session:
        print(f"✅ Redirecting to subjects page: dept={session['department']}, year={session['year']}")
        return redirect(
            url_for(
                "subjects",
                department=session["department"],
                year=session["year"],
                semester=session["semester"],
                name=session.get("name"),
                email=session.get("email"),
                section=session.get("section"),
            )
        )
    
    print("⚠️ Session incomplete, redirecting to index")
    return redirect(url_for("index"))


# @app.route("/logout")
# def logout():
#     """Logout user and clear session"""
#     session.clear()
#     return redirect(url_for("index"))

@app.route("/logout")
def logout():
    """Logout user and clear session"""
    session.clear()
    # Add a query parameter to signal clearing localStorage
    return redirect(url_for("index") + "?clear=true")

@app.route('/api/contact', methods=['POST'])
def contact_submit():
    """Handle contact form submissions"""
    try:
        data = request.json
        
        required_fields = ['name', 'email','year','section', 'subject', 'message']
        for field in required_fields:
            if not data.get(field):
                return jsonify({
                    'success': False, 
                    'message': f'Missing required field: {field}'
                }), 400
        
        contact_data = {
            'id': str(uuid.uuid4()),
            'name': data.get('name'),
            'email': data.get('email'),
            'year': data.get('year'),
            'section': data.get('section'),
            'subject': data.get('subject'),
            'message': data.get('message'),
            'timestamp': data.get('timestamp', datetime.now().isoformat()),
            'status': 'new',
            'created_at': datetime.now().isoformat()
        }
        
        try:
            contacts_key = 'contacts/submissions.json'
            try:
                contacts = s3_download_json(S3_BUCKET, contacts_key)
                if contacts is None:
                    contacts = {'submissions': []}
            except:
                contacts = {'submissions': []}
            
            contacts['submissions'].append(contact_data)
            
            s3_upload_json(S3_BUCKET, contacts_key, contacts)
            
            print(f"✅ Contact form submission saved: {contact_data['id']}")
            
            return jsonify({
                'success': True,
                'message': 'Your message has been sent successfully!'
            })
            
        except Exception as e:
            print(f"❌ Error saving contact submission: {e}")
            return jsonify({
                'success': True,
                'message': 'Your message has been received! We will get back to you soon.'
            })
            
    except Exception as e:
        print(f"❌ Contact form error: {e}")
        return jsonify({
            'success': False,
            'message': 'An error occurred. Please try again or email us directly.'
        }), 500

if __name__ == "__main__":
    if not S3_CLIENT:
        print("=" * 60)
        print("⚠️  WARNING: S3 CLIENT NOT INITIALIZED")
        print("⚠️  The application will not function properly!")
        print("⚠️  Please check your EC2 IAM role and S3 bucket access")
        print("=" * 60)

    app.run(debug=True, port=5003)