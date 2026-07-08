"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  KisanLens — Flask Backend                                                  ║
║  Version: 6.1 (Guest Mode / Try-Before-You-Buy + Account Deletion)          ║
║                                                                              ║
║  Auth     : Passwordless OTP — phone number is the unique identifier         ║
║  Storage  : Supabase (PostgreSQL) — cloud-hosted, production-grade          ║
║  OTP      : 6-digit code, 5-minute TTL, delivered via live SMS gateway      ║
║  SMS GW   : BulkBlaster MeraOTP  (asia-south1.run.app)                     ║
║  Languages: English (en) · Hindi (hi) · Marathi (mr)                       ║
║  AI Model : google/gemini-1.5-flash via google-generativeai SDK             ║
║                                                                              ║
║  NO sqlite3 · NO local .db file · NO gspread · NO google.oauth2            ║
║  NO password column · NO werkzeug hashing                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import base64
import random
from datetime import datetime, date as date_cls, timedelta
from functools import wraps

# ── Third-party ───────────────────────────────────────────────────────────────
import requests                          # live SMS gateway calls
from flask import (
    Flask, render_template, request,
    redirect, url_for, session, jsonify, flash,
)
from dotenv import load_dotenv
from supabase import create_client, Client   # Supabase Python SDK v2
import google.generativeai as genai

# ─────────────────────────────────────────────────────────────────────────────
#  App & environment
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "kisan-dev-secret-change-in-prod")

# ─────────────────────────────────────────────────────────────────────────────
#  Gemini AI setup
#  Model: gemini-1.5-flash  (fast, multimodal, handles image + text)
# ─────────────────────────────────────────────────────────────────────────────
genai.configure(api_key=os.getenv("GEMINI_API_KEY", ""))
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ─────────────────────────────────────────────────────────────────────────────
#  Supabase client — production cloud database
#
#  Required .env keys:
#    SUPABASE_URL  — e.g. https://xyzxyzxyz.supabase.co
#    SUPABASE_KEY  — the anon/service-role key from the Supabase dashboard
#
#  The `users` table must already exist in Supabase with this schema:
#    phone  text  PRIMARY KEY
#    name   text  NOT NULL
#    age    int4  NOT NULL
#    gender text  NOT NULL
#    dob    text  NOT NULL
#  (No password column — auth is fully passwordless via OTP.)
# ─────────────────────────────────────────────────────────────────────────────
_supabase_url: str = os.getenv("SUPABASE_URL", "")
_supabase_key: str = os.getenv("SUPABASE_KEY", "")

if not _supabase_url or not _supabase_key:
    raise RuntimeError(
        "[KisanLens] SUPABASE_URL and SUPABASE_KEY must be set in your .env file."
    )

supabase: Client = create_client(_supabase_url, _supabase_key)
print("✅ [KisanLens] Supabase client initialised →", _supabase_url, flush=True)

# ─────────────────────────────────────────────────────────────────────────────
#  OTP configuration
# ─────────────────────────────────────────────────────────────────────────────
OTP_DIGITS         = 6
OTP_EXPIRY_MINUTES = 5

# ── Play Store review test account (bypasses the real SMS gateway) ─────────
# Google Play reviewers cannot receive a real SMS OTP, so this fixed
# phone number + OTP pair is issued to them via Play Console → Policy →
# App content → App access. Real farmers are completely unaffected —
# this only ever matches if someone enters EXACTLY this phone number.
TEST_PHONE = "9999999999"
TEST_OTP   = "123456"

# ─────────────────────────────────────────────────────────────────────────────
#  SMS gateway — Bulk Blaster OTP API
#  Endpoint and payload keys match the official Integration Guide exactly:
#    POST https://bulkblaster-otp-api-ch-290441563653.asia-south1.run.app/...
#    Body: { "apiKey": "...", "phone": "...", "otp": "...", "brandName": "..." }
#
#  ⚠️  IMPORTANT — VERIFY THIS URL BEFORE GOING LIVE:
#  The Integration Guide screenshot truncates the full path after the numeric
#  project ID (e.g. "...asia-south1.run.app/..." is cut off on-screen).
#  Open the guide at bulkblaster.web.app/auth... on your own device, tap the
#  "Copy" button next to the Endpoint code block in Section 1, and paste the
#  FULL untruncated URL here. Do not guess the path suffix — an OTP gateway
#  with a wrong path will silently fail or 404, which is the exact symptom
#  you reported. The hostname segment is corrected below (no "mera-" prefix,
#  includes "-ch-"), but the path after the domain must be confirmed.
#
#  MERA_OTP_API_KEY must be set in .env — never hard-code it here.
# ─────────────────────────────────────────────────────────────────────────────
SMS_GATEWAY_URL     = "https://bulkblaster-otp-api-ch-290441563653.asia-south1.run.app/send-otp"  # ⚠️ confirm full path — see note above
SMS_GATEWAY_TIMEOUT = 10   # seconds — avoids hanging the login flow on a slow gateway
SMS_BRAND_NAME      = "KisanLens"

# ─────────────────────────────────────────────────────────────────────────────
#  Language helpers
# ─────────────────────────────────────────────────────────────────────────────
SUPPORTED_LANGS = {"en", "hi", "mr"}

# Full language instructions injected into every Gemini system prompt
LANG_INSTRUCTIONS = {
    "en": (
        "You MUST reply entirely in simple, clear English. "
        "Use everyday words a farmer with primary-school education can understand. "
        "Avoid technical jargon; explain any specialist term you must use."
    ),
    "hi": (
        "आपको पूरा उत्तर केवल सरल हिंदी में देना है। "
        "ऐसे शब्दों का प्रयोग करें जो एक साधारण किसान आसानी से समझ सके। "
        "कठिन शब्दों से बचें और हर तकनीकी शब्द को सरल भाषा में समझाएं।"
    ),
    "mr": (
        "तुम्ही संपूर्ण उत्तर फक्त साध्या मराठीत द्यावे. "
        "एखाद्या सामान्य शेतकऱ्याला सहज समजेल असे शब्द वापरा. "
        "तांत्रिक शब्द टाळा; वापरावे लागले तर साध्या मराठीत समजावून सांगा."
    ),
}


def resolve_lang(source) -> str:
    """
    Extract and validate the 'lang' field from a dict, form, or query string.
    Falls back to 'en' for any unsupported value.
    """
    if isinstance(source, dict):
        lang = source.get("lang", "en")
    else:
        lang = source.form.get("lang", source.args.get("lang", "en"))
    return lang if lang in SUPPORTED_LANGS else "en"


def lang_instruction(lang: str) -> str:
    return LANG_INSTRUCTIONS.get(lang, LANG_INSTRUCTIONS["en"])


# ─────────────────────────────────────────────────────────────────────────────
#  Auth decorator
# ─────────────────────────────────────────────────────────────────────────────
def login_required(f):
    """
    Page-route guard — redirects unauthenticated browsers to /login.
    Used on HTML-rendering routes only (e.g. /dashboard is now open, so this
    decorator is no longer applied there).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    """
    API-route guard — returns HTTP 401 JSON to unauthenticated fetch() calls.

    Applied to every /api/* processing endpoint so that a guest who bypasses
    the UI and sends a raw POST still receives a clean, machine-readable error
    instead of an HTML redirect they cannot handle.

    Response body:  { "error": "Authentication required.", "auth_required": true }
    The frontend reads `auth_required: true` and shows the guest modal instead
    of displaying a raw error string.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({
                "error":         "Authentication required. Please log in or sign up.",
                "auth_required": True,
            }), 401
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
#  Page routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """
    Play Store launch entry point — always requires authentication first.
    Logged-in users go straight to the dashboard; everyone else sees the
    login page (with a link to /signup for new farmers).
    """
    if "user" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login")
def login():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """
    Passwordless registration.
    Fields: name, phone, age, gender, dob.
    On success → insert into Supabase and auto-login to dashboard.
    """
    if request.method == "POST":
        name   = request.form.get("name",   "").strip()
        phone  = request.form.get("phone",  "").strip()
        age    = request.form.get("age",    "").strip()
        gender = request.form.get("gender", "").strip()
        dob    = request.form.get("dob",    "").strip()

        # Validation
        if not all([name, phone, age, gender, dob]):
            flash("All fields are required.", "error")
            return render_template("signup.html")

        if not phone.isdigit() or len(phone) != 10:
            flash("Phone number must be exactly 10 digits.", "error")
            return render_template("signup.html")

        try:
            age_int = int(age)
            if not (16 <= age_int <= 100):
                raise ValueError
        except ValueError:
            flash("Please enter a valid age between 16 and 100.", "error")
            return render_template("signup.html")

        # ── Persist to Supabase ───────────────────────────────────────────────
        try:
            supabase.table("users").insert({
                "phone":  phone,
                "name":   name,
                "age":    age_int,
                "gender": gender,
                "dob":    dob,
            }).execute()

        except Exception as exc:
            err_str = str(exc).lower()
            # Supabase / PostgREST raises a generic exception whose message
            # contains "duplicate key" or "unique" when the PRIMARY KEY (phone)
            # already exists in the table.
            if "duplicate" in err_str or "unique" in err_str or "23505" in err_str:
                flash("This phone number is already registered. Please log in.", "error")
            else:
                app.logger.error("[signup] Supabase insert error: %s", exc)
                flash("Registration failed due to a server error. Please try again.", "error")
            return render_template("signup.html")

        # Auto-login
        session["user"] = {
            "name": name, "phone": phone,
            "age": str(age_int), "gender": gender, "dob": dob,
        }
        flash(f"Welcome to KisanLens, {name}! 🌿", "success")
        return redirect(url_for("dashboard"))

    return render_template("signup.html")


@app.route("/dashboard")
@login_required
def dashboard():
    """
    Play Store launch version — requires authentication.
    Guest browsing mode has been removed: a farmer must complete the
    phone + OTP login or signup flow before reaching the dashboard.
    is_guest is always False here since @login_required guarantees a
    session exists, but the flag is still passed so dashboard.html's
    existing Jinja conditionals keep working without further edits.
    """
    return render_template(
        "dashboard.html",
        user=session["user"],
        is_guest=False,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────────────────────────────────────
#  OTP routes (JSON API — called via fetch() from login.html)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/send-otp", methods=["POST"])
def send_otp():
    """
    Step 1 of passwordless login — Production SMS Gateway integration.
    Body (JSON): { "phone": "9876543210" }

    Execution flow:
      1. Validate the phone number format (10 digits, numeric).
      2. Confirm the phone exists in the Supabase `users` table.
      3. Generate a cryptographically-adequate 6-digit OTP via random.randint.
      4. Store { phone, otp, expiry } in the server-side Flask session.
         The OTP value is NEVER included in any HTTP response body.
      5. Dispatch the OTP to the farmer's handset via the BulkBlaster MeraOTP
         gateway using a requests.post() call wrapped in a try/except block
         that handles connection errors, timeouts, and non-2xx gateway responses.
      6. On gateway success  → return { success: true }  to transition the
         frontend to the OTP input step.
         On gateway failure  → log the error server-side and return a clear,
         user-friendly error message to the UI (never expose raw exception text).

    EXCEPTION — Play Store review test account:
      If the phone number matches TEST_PHONE exactly, the real SMS gateway
      call is skipped entirely and a fixed OTP (TEST_OTP) is stored instead.
      This only triggers for that one exact number — every other phone number
      goes through the full real flow below, unchanged.
    """
    body  = request.get_json(silent=True) or {}
    phone = str(body.get("phone", "")).strip()

    # ── Step 1: Validate phone format ─────────────────────────────────────────
    if not phone.isdigit() or len(phone) != 10:
        return jsonify({
            "success": False,
            "error":   "Enter a valid 10-digit phone number.",
        }), 400

    # ── Step 2: Confirm phone exists in the Supabase `users` table ───────────
    try:
        user_query = (
            supabase.table("users")
            .select("phone")
            .eq("phone", phone)
            .execute()
        )
    except Exception as db_exc:
        app.logger.error("[send-otp] Supabase query error: %s", db_exc)
        return jsonify({
            "success": False,
            "error":   "A server error occurred. Please try again.",
        }), 500

    # user_query.data is a list; an empty list means the phone isn't registered
    if not user_query.data:
        return jsonify({
            "success": False,
            "error":   "Phone number not found. Please sign up first.",
        }), 404

    # ── Play Store reviewer bypass — skip the real SMS gateway entirely ──────
    # Only fires for the exact TEST_PHONE number. You must first add a row
    # for this phone number in the Supabase `users` table (see setup notes),
    # otherwise Step 2 above will already have rejected it as "not found".
    if phone == TEST_PHONE:
        expiry = (datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()
        session["otp_data"] = {"phone": phone, "otp": TEST_OTP, "expiry": expiry}
        masked = phone[:2] + ("*" * 6) + phone[-2:]
        app.logger.info("[send-otp] Play Store test OTP issued (SMS gateway skipped)")
        return jsonify({
            "success": True,
            "message": f"OTP sent to {masked}",
        })

    # ── Step 3: Generate OTP ──────────────────────────────────────────────────
    otp    = str(random.randint(10 ** (OTP_DIGITS - 1), 10 ** OTP_DIGITS - 1))
    expiry = (datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()

    # ── Step 4: Persist OTP in server-side session (never sent to client) ─────
    session["otp_data"] = {"phone": phone, "otp": otp, "expiry": expiry}

    # ── Step 5: Dispatch OTP via the Bulk Blaster OTP gateway ─────────────────
    # Field names match the Integration Guide exactly: apiKey, phone, otp, brandName.
    # (Earlier versions of this code sent "mobileNo" + "messageType", which do
    # NOT match the documented contract and is why OTPs were not arriving.)
    payload = {
        "apiKey":    os.getenv("MERA_OTP_API_KEY"),   # loaded from .env
        "phone":     phone,
        "otp":       otp,
        "brandName": SMS_BRAND_NAME,                  # "KisanLens"
    }
    headers = {"Content-Type": "application/json"}

    try:
        gw_response = requests.post(
            SMS_GATEWAY_URL,
            json=payload,               # serialises dict → JSON body automatically
            headers=headers,
            timeout=SMS_GATEWAY_TIMEOUT,
        )

        # Treat any non-2xx HTTP status as a gateway-level failure
        gw_response.raise_for_status()

        # ── Inspect the response BODY too ──────────────────────────────────────
        # This gateway returns HTTP 200 even for business-logic failures such as
        # {"success": false, "error": "Insufficient balance."} — raise_for_status()
        # alone does NOT catch this, so without this check a failed send would
        # be silently treated as a success and the farmer would never get an OTP.
        gw_body = {}
        try:
            gw_body = gw_response.json()
        except ValueError:
            app.logger.error("[send-otp] Gateway returned non-JSON body: %s", gw_response.text[:200])

        if gw_body.get("success") is False:
            gateway_error = gw_body.get("error", "Unknown gateway error.")
            app.logger.error("[send-otp] Gateway rejected send: %s", gateway_error)
            session.pop("otp_data", None)
            return jsonify({
                "success": False,
                "error":   f"SMS could not be sent: {gateway_error}",
            }), 502

        app.logger.info("[send-otp] SMS gateway accepted OTP for %s — status %s",
                        phone[:2] + "******" + phone[-2:], gw_response.status_code)

    except requests.exceptions.Timeout:
        # Gateway did not respond within SMS_GATEWAY_TIMEOUT seconds
        app.logger.error("[send-otp] SMS gateway timed out for phone ending %s", phone[-4:])
        session.pop("otp_data", None)   # discard OTP — it was never delivered
        return jsonify({
            "success": False,
            "error":   "SMS gateway timed out. Please try again in a moment.",
        }), 503

    except requests.exceptions.ConnectionError as conn_err:
        # Network-level failure (DNS, refused connection, etc.)
        app.logger.error("[send-otp] SMS gateway connection error: %s", conn_err)
        session.pop("otp_data", None)
        return jsonify({
            "success": False,
            "error":   "Could not reach the SMS gateway. Check your network and retry.",
        }), 503

    except requests.exceptions.HTTPError as http_err:
        # Gateway returned 4xx / 5xx
        status = gw_response.status_code if gw_response else "unknown"
        app.logger.error("[send-otp] SMS gateway HTTP %s error: %s", status, http_err)
        session.pop("otp_data", None)
        return jsonify({
            "success": False,
            "error":   f"SMS gateway rejected the request (HTTP {status}). Please contact support.",
        }), 502

    except requests.exceptions.RequestException as req_err:
        # Catch-all for any other requests library exception
        app.logger.error("[send-otp] Unexpected SMS gateway error: %s", req_err)
        session.pop("otp_data", None)
        return jsonify({
            "success": False,
            "error":   "An unexpected error occurred while sending the OTP. Please try again.",
        }), 500

    # ── Step 6: Return success — mask phone number for the UI display ─────────
    masked = phone[:2] + ("*" * 6) + phone[-2:]
    return jsonify({
        "success": True,
        "message": f"OTP sent to {masked}",
    })


@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    """
    Step 2 of passwordless login.
    Body: { "phone": "9876543210", "otp": "482917" }

    Checks: session exists → phone matches → not expired → OTP correct.
    On success: clears OTP from session, sets session["user"], returns redirect URL.

    No special-casing is needed here for the Play Store test account — it
    already went through the exact same session-based OTP storage in
    send_otp(), so it is verified by the same code path as every real user.
    """
    body      = request.get_json(silent=True) or {}
    phone     = str(body.get("phone", "")).strip()
    otp_input = str(body.get("otp",   "")).strip()

    otp_data = session.get("otp_data")

    if not otp_data:
        return jsonify({"success": False,
                        "error": "Session expired. Please request a new OTP."}), 400

    if otp_data.get("phone") != phone:
        return jsonify({"success": False, "error": "Phone number mismatch."}), 400

    try:
        expiry_dt = datetime.fromisoformat(otp_data["expiry"])
    except (KeyError, ValueError):
        session.pop("otp_data", None)
        return jsonify({"success": False,
                        "error": "Invalid session data. Please try again."}), 400

    if datetime.utcnow() > expiry_dt:
        session.pop("otp_data", None)
        return jsonify({"success": False,
                        "error": "OTP has expired. Please request a new one."}), 400

    if otp_data["otp"] != otp_input:
        return jsonify({"success": False,
                        "error": "Incorrect OTP. Please try again."}), 401

    # ── OTP verified — fetch full user record from Supabase and build session ──
    try:
        user_query = (
            supabase.table("users")
            .select("*")
            .eq("phone", phone)
            .execute()
        )
    except Exception as exc:
        app.logger.error("[verify-otp] Supabase fetch error: %s", exc)
        return jsonify({"success": False, "error": "A server error occurred. Please try again."}), 500

    if not user_query.data:
        return jsonify({"success": False, "error": "User not found."}), 404

    user = user_query.data[0]   # data is a list of dicts; grab the first (and only) match

    session.pop("otp_data", None)
    session["user"] = {
        "name":   user["name"],
        "phone":  user["phone"],
        "age":    str(user["age"]),
        "gender": user["gender"],
        "dob":    user["dob"],
    }
    return jsonify({"success": True, "redirect": url_for("dashboard")})


# ─────────────────────────────────────────────────────────────────────────────
#  Account deletion (Play Store Data Safety requirement)
#
#  Google requires a publicly reachable page (no login needed to VIEW it)
#  that explains how a user can request deletion of their account and data.
#  Since KisanLens is fully passwordless, the deletion action itself is
#  gated behind the same OTP proof-of-ownership used for login — this
#  keeps a stranger from deleting someone else's account just by knowing
#  their phone number.
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/delete-account", methods=["GET"])
def delete_account_page():
    """
    Public info + self-service page — intentionally has NO @login_required
    so that Google Play reviewers (and logged-out farmers) can load it and
    get a 200 response. Renders templates/delete_account.html.
    """
    return render_template("delete_account.html")


@app.route("/api/delete-account/request-otp", methods=["POST"])
def delete_account_request_otp():
    """
    Body (JSON): { "phone": "9876543210" }

    Sends an OTP to prove ownership of the phone number before deletion.
    Uses a SEPARATE session key ("delete_otp_data") from the login OTP
    ("otp_data") so an in-progress login and an in-progress deletion
    request never collide with each other.
    """
    body  = request.get_json(silent=True) or {}
    phone = str(body.get("phone", "")).strip()

    if not phone.isdigit() or len(phone) != 10:
        return jsonify({"success": False, "error": "Enter a valid 10-digit phone number."}), 400

    try:
        user_query = (
            supabase.table("users")
            .select("phone")
            .eq("phone", phone)
            .execute()
        )
    except Exception as db_exc:
        app.logger.error("[delete-account] Supabase query error: %s", db_exc)
        return jsonify({"success": False, "error": "A server error occurred. Please try again."}), 500

    if not user_query.data:
        return jsonify({"success": False, "error": "No account found with this phone number."}), 404

    otp    = str(random.randint(10 ** (OTP_DIGITS - 1), 10 ** OTP_DIGITS - 1))
    expiry = (datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()
    session["delete_otp_data"] = {"phone": phone, "otp": otp, "expiry": expiry}

    payload = {
        "apiKey":    os.getenv("MERA_OTP_API_KEY"),
        "phone":     phone,
        "otp":       otp,
        "brandName": SMS_BRAND_NAME,
    }
    headers = {"Content-Type": "application/json"}

    try:
        gw_response = requests.post(
            SMS_GATEWAY_URL,
            json=payload,
            headers=headers,
            timeout=SMS_GATEWAY_TIMEOUT,
        )
        gw_response.raise_for_status()

        gw_body = {}
        try:
            gw_body = gw_response.json()
        except ValueError:
            app.logger.error("[delete-account] Gateway returned non-JSON body: %s", gw_response.text[:200])

        if gw_body.get("success") is False:
            gateway_error = gw_body.get("error", "Unknown gateway error.")
            app.logger.error("[delete-account] Gateway rejected send: %s", gateway_error)
            session.pop("delete_otp_data", None)
            return jsonify({"success": False, "error": f"SMS could not be sent: {gateway_error}"}), 502

    except requests.exceptions.Timeout:
        app.logger.error("[delete-account] SMS gateway timed out for phone ending %s", phone[-4:])
        session.pop("delete_otp_data", None)
        return jsonify({"success": False, "error": "SMS gateway timed out. Please try again."}), 503

    except requests.exceptions.ConnectionError as conn_err:
        app.logger.error("[delete-account] SMS gateway connection error: %s", conn_err)
        session.pop("delete_otp_data", None)
        return jsonify({"success": False, "error": "Could not reach the SMS gateway. Please retry."}), 503

    except requests.exceptions.HTTPError as http_err:
        status = gw_response.status_code if gw_response else "unknown"
        app.logger.error("[delete-account] SMS gateway HTTP %s error: %s", status, http_err)
        session.pop("delete_otp_data", None)
        return jsonify({"success": False, "error": f"SMS gateway rejected the request (HTTP {status})."}), 502

    except requests.exceptions.RequestException as req_err:
        app.logger.error("[delete-account] Unexpected SMS gateway error: %s", req_err)
        session.pop("delete_otp_data", None)
        return jsonify({"success": False, "error": "An unexpected error occurred. Please try again."}), 500

    masked = phone[:2] + ("*" * 6) + phone[-2:]
    return jsonify({"success": True, "message": f"OTP sent to {masked}"})


@app.route("/api/delete-account/confirm", methods=["POST"])
def delete_account_confirm():
    """
    Body (JSON): { "phone": "9876543210", "otp": "482917" }

    Verifies the deletion OTP, then permanently deletes the user's row
    from Supabase. If the requester is currently logged in as that same
    phone number, their session is cleared too.
    """
    body      = request.get_json(silent=True) or {}
    phone     = str(body.get("phone", "")).strip()
    otp_input = str(body.get("otp", "")).strip()

    otp_data = session.get("delete_otp_data")

    if not otp_data:
        return jsonify({"success": False, "error": "Session expired. Please request a new OTP."}), 400

    if otp_data.get("phone") != phone:
        return jsonify({"success": False, "error": "Phone number mismatch."}), 400

    try:
        expiry_dt = datetime.fromisoformat(otp_data["expiry"])
    except (KeyError, ValueError):
        session.pop("delete_otp_data", None)
        return jsonify({"success": False, "error": "Invalid session data. Please try again."}), 400

    if datetime.utcnow() > expiry_dt:
        session.pop("delete_otp_data", None)
        return jsonify({"success": False, "error": "OTP has expired. Please request a new one."}), 400

    if otp_data["otp"] != otp_input:
        return jsonify({"success": False, "error": "Incorrect OTP. Please try again."}), 401

    try:
        supabase.table("users").delete().eq("phone", phone).execute()
    except Exception as exc:
        app.logger.error("[delete-account] Supabase delete error: %s", exc)
        return jsonify({"success": False, "error": "Deletion failed due to a server error. Please try again."}), 500

    session.pop("delete_otp_data", None)

    # If the person deleting is also currently logged in as this phone,
    # end their session too so they aren't left in a stale logged-in state.
    if session.get("user", {}).get("phone") == phone:
        session.clear()

    app.logger.info("[delete-account] Account deleted for phone ending %s", phone[-4:])
    return jsonify({"success": True, "message": "Your account and all associated data have been permanently deleted."})


# ─────────────────────────────────────────────────────────────────────────────
#  AI helper utilities
# ─────────────────────────────────────────────────────────────────────────────

def read_image(field: str = "image"):
    """
    Read an uploaded image from request.files[field].
    Returns (base64_string, mime_type) or raises ValueError.
    """
    if field not in request.files:
        raise ValueError("No image file in the request.")
    img = request.files[field]
    if not img or img.filename == "":
        raise ValueError("Image file is empty or has no filename.")
    raw  = img.read()
    b64  = base64.b64encode(raw).decode("utf-8")
    mime = img.content_type or "image/jpeg"
    return b64, mime


def gemini_vision(prompt: str, b64: str, mime: str) -> str:
    """Send a prompt + base64 image to Gemini and return the text response."""
    response = gemini_model.generate_content([
        prompt,
        {"mime_type": mime, "data": b64},
    ])
    return response.text


# ─────────────────────────────────────────────────────────────────────────────
#  AI Endpoint 1 — Crop Doctor
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/crop-doctor", methods=["POST"])
@api_login_required
def crop_doctor():
    """
    POST  multipart/form-data
      image : photo of a diseased crop leaf or plant
      lang  : 'en' | 'hi' | 'mr'   (form field or query param)

    The AI diagnoses the problem and outputs ONLY generic chemical
    active ingredients with exact quantities — no brand names.
    """
    lang = request.form.get("lang", request.args.get("lang", "en"))
    if lang not in SUPPORTED_LANGS:
        lang = "en"

    try:
        b64, mime = read_image("image")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    acres_raw = request.form.get("acres", "1").strip()
    try:
        acres = max(0.25, float(acres_raw))
    except ValueError:
        acres = 1.0

    spray_tank_litres = 15    # standard knapsack sprayer

    prompt = f"""{lang_instruction(lang)}

You are an expert agricultural scientist and plant pathologist helping a Maharashtra farmer.
Analyze this crop image carefully. Respond ENTIRELY in the language specified above.

SECTION 1 — 🌿 पीक ओळख व रोग निदान (CROP IDENTIFICATION & DIAGNOSIS)
• Identify the exact crop shown in the image.
• Identify the disease, pest, or nutrient deficiency visible.
• Give the disease name in Marathi + English in brackets: e.g. "पांढरी माशी (Whitefly)"
• Rate severity: सौम्य (Mild) / मध्यम (Moderate) / तीव्र (Severe)

SECTION 2 — 💊 रासायनिक उपचार (CHEMICAL TREATMENT)
For each recommended generic chemical:
• Show as: मराठी नाव (English Name) — e.g. इमिडाक्लोप्रिड (Imidacloprid) 17.8% SL
• प्रति एकर मात्रा (Dosage per Acre): X ml or grams
• तुमच्यासाठी ({acres} एकर): calculate → X × {acres} = TOTAL ml or grams
• पाणी (Water): {acres} × 200 = TOTAL liters
• स्प्रे टाक्या (Spray Tanks at {spray_tank_litres}L each): divide water by {spray_tank_litres}
• NEVER use brand names. Generic active ingredients only.

SECTION 3 — 🌿 घरगुती / सेंद्रिय उपाय (HOMEMADE ORGANIC SOLUTION)
Give 2 traditional Maharashtrian organic remedies for this specific disease:
For each remedy show:
• नाव (Name): e.g. निंबोळी अर्क (Neem Extract Spray)
• साहित्य (Ingredients): list items available in villages
• तयार करण्याची पद्धत (Preparation): step-by-step simple Marathi
• किती लागेल ({acres} एकरसाठी): quantity needed
• कधी व कसे फवारावे (When & How to Spray)

SECTION 4 — ⚠️ सुरक्षा सूचना (SAFETY PRECAUTIONS)
Exactly 3 safety rules in simple Marathi for a village farmer, each starting with •

Keep all language simple and practical. Use the • bullet character at the start of EVERY
line item (not -, not *, not numbers) so the app can format your answer into clean cards.
Put each bullet point on its own line. Do not use markdown bold (**) or headers (#) —
plain text with • bullets only.
Acre field size for calculation: {acres} acres.
"""

    try:
        return jsonify({"result": gemini_vision(prompt, b64, mime), "acres": acres})
    except Exception as exc:
        return jsonify({"error": f"AI error: {exc}"}), 500


# ─────────────────────────────────────────────────────────────────────────────
#  AI Endpoint 2 — Label Scanner
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/label-scanner", methods=["POST"])
@api_login_required
def label_scanner():
    """
    POST  multipart/form-data
      image : photo of a fertiliser / pesticide container label
      lang  : 'en' | 'hi' | 'mr'
      acres : farmer's field size, used to size the generic-equivalent dosage

    The AI extracts active ingredients, explains them simply, shows
    the farmer how to buy the same chemistry as a cheap generic product,
    and scales the recommended quantity to the farmer's actual field size.
    """
    lang = request.form.get("lang", request.args.get("lang", "en"))
    if lang not in SUPPORTED_LANGS:
        lang = "en"

    acres_raw = request.form.get("acres", "1").strip()
    try:
        acres = max(0.25, float(acres_raw))
    except ValueError:
        acres = 1.0

    try:
        b64, mime = read_image("image")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    prompt = f"""{lang_instruction(lang)}

You are an agricultural chemical expert helping a Maharashtra farmer understand product labels and save money.
Analyze the pesticide or fertilizer label in this image. Respond ENTIRELY in the language specified above.
The farmer's field size is {acres} acres — use this for all quantity calculations below.

SECTION 1 — 🏷️ रासायनिक माहिती (CHEMICAL INFORMATION)
• उत्पादनाचे नाव (Product Name): what is written on the label
• सक्रिय घटक (Active Ingredients): list each with Marathi name + English in brackets + concentration %
  Example: मॅन्कोझेब (Mancozeb) 75% WP
• हे कशासाठी वापरतात (What it does): one simple Marathi sentence per ingredient
• उपयोग (Purpose/Use): fungicide / insecticide / herbicide / fertilizer

SECTION 2 — 💰 स्वस्त पर्याय (GENERIC ALTERNATIVES)
• हेच घटक कमी किमतीत मिळतात (Generic equivalents available at agri shops)
• Show 2-3 generic chemical names they can ask for without brand markup
• प्रति एकर मात्रा (Dosage per Acre): give the standard dose for this chemical
• तुमच्यासाठी ({acres} एकर): calculate total quantity needed = dosage × {acres}
• किती बचत होईल (Estimated savings): % or ₹ amount vs brand price
• सांगा: "हे घटक तुमच्या जवळच्या कृषी सेवा केंद्रात स्वस्तात मिळतात — ब्रँडसाठी जास्त पैसे देऊ नका"

SECTION 3 — 🌿 घरगुती पर्याय (HOMEMADE ALTERNATIVES)
Based on the PURPOSE of this product, suggest 2 traditional organic alternatives:
• नाव (Name) + साहित्य (Ingredients) + तयार करण्याची पद्धत (Preparation steps)
• किती लागेल ({acres} एकरसाठी): quantity needed for the farmer's field size
• कधी व कसे वापरावे (When & how to use)

SECTION 4 — ⚠️ महत्त्वाच्या सूचना (IMPORTANT WARNINGS)
• कोणत्या रसायनांसोबत मिसळू नये (What NOT to mix with)
• साठवणूक कशी करावी (Storage instructions)
• सुरक्षा उपाय (Safety measures): 2-3 points

Use simple village-level Marathi. Show English name in brackets when helpful.
Use the • bullet character at the start of EVERY line item (not -, not *, not numbers)
so the app can format your answer into clean cards. Put each bullet point on its own
line. Do not use markdown bold (**) or headers (#) — plain text with • bullets only.
"""

    try:
        return jsonify({"result": gemini_vision(prompt, b64, mime), "acres": acres})
    except Exception as exc:
        return jsonify({"error": f"AI error: {exc}"}), 500


# ─────────────────────────────────────────────────────────────────────────────
#  AI Endpoint 3 — Crop Roadmap (Calendar)
# ─────────────────────────────────────────────────────────────────────────────

CROP_CALENDARS = {
    "rice": [
        {"day":   0, "event": "Transplanting / Sowing",               "type": "sow",        "icon": "🌱"},
        {"day":   7, "event": "First irrigation",                      "type": "water",      "icon": "💧"},
        {"day":  15, "event": "Apply Urea — 1st dose",                 "type": "fertilizer", "icon": "🧪"},
        {"day":  21, "event": "Weeding — 1st round",                   "type": "weed",       "icon": "🌿"},
        {"day":  30, "event": "Pest & disease inspection",             "type": "inspect",    "icon": "🔍"},
        {"day":  45, "event": "Apply DAP — 2nd fertilizer dose",       "type": "fertilizer", "icon": "🧪"},
        {"day":  60, "event": "Weeding — 2nd round",                   "type": "weed",       "icon": "🌿"},
        {"day":  90, "event": "Stop irrigation (10 days pre-harvest)", "type": "water",      "icon": "🚫💧"},
        {"day": 110, "event": "Harvest",                               "type": "harvest",    "icon": "🌾"},
    ],
    "wheat": [
        {"day":   0, "event": "Sowing",                                "type": "sow",        "icon": "🌱"},
        {"day":  21, "event": "Crown Root Irrigation (CRI)",           "type": "water",      "icon": "💧"},
        {"day":  25, "event": "Apply Urea — top dressing",             "type": "fertilizer", "icon": "🧪"},
        {"day":  40, "event": "2nd irrigation",                        "type": "water",      "icon": "💧"},
        {"day":  60, "event": "3rd irrigation + weed check",           "type": "weed",       "icon": "🌿"},
        {"day":  80, "event": "4th irrigation",                        "type": "water",      "icon": "💧"},
        {"day": 100, "event": "Stop irrigation",                       "type": "water",      "icon": "🚫💧"},
        {"day": 120, "event": "Harvest",                               "type": "harvest",    "icon": "🌾"},
    ],
    "tomato": [
        {"day":  0, "event": "Transplanting seedlings",                "type": "sow",        "icon": "🌱"},
        {"day":  3, "event": "Light irrigation",                       "type": "water",      "icon": "💧"},
        {"day": 10, "event": "Apply starter fertilizer",               "type": "fertilizer", "icon": "🧪"},
        {"day": 20, "event": "Staking / support setup",                "type": "inspect",    "icon": "🔧"},
        {"day": 25, "event": "Weeding — 1st round",                    "type": "weed",       "icon": "🌿"},
        {"day": 35, "event": "2nd fertilizer dose",                    "type": "fertilizer", "icon": "🧪"},
        {"day": 45, "event": "Flower set — inspect for pest",          "type": "inspect",    "icon": "🔍"},
        {"day": 60, "event": "First harvest (green stage)",            "type": "harvest",    "icon": "🍅"},
        {"day": 75, "event": "Main harvest season",                    "type": "harvest",    "icon": "🌾"},
    ],
    "onion": [
        {"day":   0, "event": "Transplanting",                         "type": "sow",        "icon": "🌱"},
        {"day":   7, "event": "Light irrigation",                      "type": "water",      "icon": "💧"},
        {"day":  15, "event": "Apply Urea — 1st dose",                 "type": "fertilizer", "icon": "🧪"},
        {"day":  30, "event": "Weeding — 1st round",                   "type": "weed",       "icon": "🌿"},
        {"day":  45, "event": "Apply MOP (Potash)",                    "type": "fertilizer", "icon": "🧪"},
        {"day":  60, "event": "Weeding — 2nd round",                   "type": "weed",       "icon": "🌿"},
        {"day":  90, "event": "Reduce irrigation frequency",           "type": "water",      "icon": "💧"},
        {"day": 100, "event": "Stop irrigation (neck fall stage)",     "type": "water",      "icon": "🚫💧"},
        {"day": 110, "event": "Harvest",                               "type": "harvest",    "icon": "🌾"},
    ],
    "soybean": [
        {"day":  0, "event": "Sowing",                                 "type": "sow",        "icon": "🌱"},
        {"day":  7, "event": "Pre-emergence herbicide application",    "type": "fertilizer", "icon": "🧪"},
        {"day": 20, "event": "First irrigation",                       "type": "water",      "icon": "💧"},
        {"day": 25, "event": "Inter-cultivation / weeding",            "type": "weed",       "icon": "🌿"},
        {"day": 35, "event": "Spray for stem fly / girdle beetle",     "type": "inspect",    "icon": "🔍"},
        {"day": 50, "event": "Flowering stage — stop all herbicides",  "type": "inspect",    "icon": "🔍"},
        {"day": 70, "event": "Pod-fill irrigation",                    "type": "water",      "icon": "💧"},
        {"day": 95, "event": "Harvest when 95% pods turn brown",      "type": "harvest",    "icon": "🌾"},
    ],
    "cotton": [
        {"day":   0, "event": "Sowing",                                "type": "sow",        "icon": "🌱"},
        {"day":  10, "event": "Gap filling / thinning",                "type": "inspect",    "icon": "🔍"},
        {"day":  20, "event": "Apply basal fertilizer",                "type": "fertilizer", "icon": "🧪"},
        {"day":  30, "event": "Weeding — 1st round",                   "type": "weed",       "icon": "🌿"},
        {"day":  45, "event": "Apply Urea — top dressing",             "type": "fertilizer", "icon": "🧪"},
        {"day":  60, "event": "Squaring stage — pest monitoring",      "type": "inspect",    "icon": "🔍"},
        {"day":  75, "event": "Boll formation — irrigation",           "type": "water",      "icon": "💧"},
        {"day": 120, "event": "1st picking",                           "type": "harvest",    "icon": "🌾"},
        {"day": 150, "event": "2nd picking",                           "type": "harvest",    "icon": "🌾"},
    ],
}

DEFAULT_CALENDAR = [
    {"day":  0, "event": "Sowing / Planting",          "type": "sow",        "icon": "🌱"},
    {"day": 10, "event": "First irrigation",            "type": "water",      "icon": "💧"},
    {"day": 20, "event": "Apply base fertilizer",       "type": "fertilizer", "icon": "🧪"},
    {"day": 35, "event": "Weeding",                     "type": "weed",       "icon": "🌿"},
    {"day": 60, "event": "2nd fertilizer dose",         "type": "fertilizer", "icon": "🧪"},
    {"day": 75, "event": "Pest and disease inspection", "type": "inspect",    "icon": "🔍"},
    {"day": 90, "event": "Harvest readiness check",    "type": "harvest",    "icon": "🌾"},
]


def tag_milestones(milestones: list, crop_age: int) -> list:
    """
    Annotate each milestone dict with status:
      'done'     — more than 5 days past the milestone day
      'current'  — within a 5-day window of the milestone day
      'upcoming' — hasn't arrived yet (adds days_left key)
    """
    result = []
    for m in milestones:
        entry = dict(m)
        if crop_age >= m["day"] + 5:
            entry["status"] = "done"
        elif crop_age >= m["day"]:
            entry["status"] = "current"
        else:
            entry["status"]    = "upcoming"
            entry["days_left"] = m["day"] - crop_age
        result.append(entry)
    return result


@app.route("/api/crop-roadmap", methods=["POST"])
@api_login_required
def crop_roadmap():
    """
    POST  JSON  { "crop_type": "rice", "planting_date": "2025-03-01", "lang": "en" }

    Returns the milestone calendar for the crop with each item tagged as
    done / current / upcoming.  Also returns a short Gemini-generated
    AI tip for the current growth stage in the requested language.
    """
    body         = request.get_json(silent=True) or {}
    crop_type    = str(body.get("crop_type", "")).strip().lower()
    planting_str = str(body.get("planting_date", "")).strip()
    lang         = resolve_lang(body)

    if not crop_type or not planting_str:
        return jsonify({"error": "Both crop_type and planting_date are required."}), 400

    try:
        planting_date = datetime.strptime(planting_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date. Use YYYY-MM-DD format."}), 400

    crop_age = (date_cls.today() - planting_date).days
    if crop_age < 0:
        return jsonify({"error": "Planting date cannot be in the future."}), 400

    raw_milestones = CROP_CALENDARS.get(crop_type, DEFAULT_CALENDAR)
    milestones     = tag_milestones(raw_milestones, crop_age)

    # Current stage label for the AI tip
    current_labels = [m["event"] for m in milestones if m["status"] == "current"]
    current_label  = current_labels[0] if current_labels else "general crop care"

    # AI tip — rich Marathi-first with specific activities for the stage
    ai_tip = ""
    try:
        tip_prompt = (
            f"{lang_instruction(lang)}\n\n"
            f"A Maharashtra farmer has a {crop_type} crop that is {crop_age} days old. "
            f"Current stage: '{current_label}'. "
            f"Give a practical stage-specific guide including: "
            f"1) खते / पाणी (Fertilizer & Irrigation advice with quantities), "
            f"2) कीड / रोग सतर्कता (Disease/pest alert common at this stage), "
            f"3) या आठवड्यातील कामे (This week's key activities). "
            f"Keep it under 80 words. Use simple Marathi. Show chemical names in मराठी (English) format."
        )
        ai_tip = gemini_model.generate_content(tip_prompt).text.strip()
    except Exception:
        ai_tip = ""

    return jsonify({
        "crop_type":     crop_type,
        "planting_date": planting_str,
        "crop_age_days": crop_age,
        "milestones":    milestones,
        "ai_tip":        ai_tip,
        "lang":          lang,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  API — Mandi / Market Prices
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/mandi-prices", methods=["GET"])
@api_login_required
def mandi_prices():
    """
    Mock wholesale mandi (market) prices for major Indian crops.
    Production replacement: data.gov.in / Agmarknet API.
    """
    prices = [
        {"crop": "Rice (Basmati)", "price": 3850,  "unit": "Rs/quintal", "trend": "up",   "change": "+120", "market": "Amravati"},
        {"crop": "Wheat",          "price": 2250,  "unit": "Rs/quintal", "trend": "down", "change": "-40",  "market": "Nagpur"},
        {"crop": "Tomato",         "price":  920,  "unit": "Rs/quintal", "trend": "up",   "change": "+210", "market": "Pune"},
        {"crop": "Onion",          "price": 1400,  "unit": "Rs/quintal", "trend": "up",   "change": "+80",  "market": "Nashik"},
        {"crop": "Soybean",        "price": 4500,  "unit": "Rs/quintal", "trend": "down", "change": "-150", "market": "Akola"},
        {"crop": "Cotton",         "price": 6800,  "unit": "Rs/quintal", "trend": "up",   "change": "+300", "market": "Yavatmal"},
        {"crop": "Turmeric",       "price": 9200,  "unit": "Rs/quintal", "trend": "up",   "change": "+500", "market": "Sangli"},
        {"crop": "Sugarcane",      "price":  350,  "unit": "Rs/quintal", "trend": "same", "change": "0",    "market": "Kolhapur"},
        {"crop": "Chilli (Dry)",   "price": 12500, "unit": "Rs/quintal", "trend": "down", "change": "-200", "market": "Guntur"},
        {"crop": "Potato",         "price":  750,  "unit": "Rs/quintal", "trend": "down", "change": "-30",  "market": "Indore"},
        {"crop": "Maize",          "price": 1950,  "unit": "Rs/quintal", "trend": "up",   "change": "+60",  "market": "Amravati"},
        {"crop": "Groundnut",      "price": 5600,  "unit": "Rs/quintal", "trend": "same", "change": "0",    "market": "Junagadh"},
    ]
    return jsonify({
        "prices":       prices,
        "last_updated": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  API — Government Schemes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/schemes", methods=["GET"])
@api_login_required
def schemes():
    """
    Filter government agricultural schemes by the logged-in farmer's
    age and gender (stored in session).
    """
    user_age    = int(session["user"].get("age", 0) or 0)
    user_gender = session["user"].get("gender", "").lower()

    all_schemes = [
        {
            "name":        "PM-KISAN Samman Nidhi",
            "benefit":     "Rs 6,000/year direct income — 3 instalments of Rs 2,000",
            "eligibility": "All small and marginal landholding farmers",
            "link":        "https://pmkisan.gov.in",
            "min_age": 18, "max_age": 99, "gender": "all",
        },
        {
            "name":        "PM Fasal Bima Yojana (PMFBY)",
            "benefit":     "Subsidised crop insurance against calamities, pests, and disease",
            "eligibility": "All farmers growing notified crops",
            "link":        "https://pmfby.gov.in",
            "min_age": 18, "max_age": 99, "gender": "all",
        },
        {
            "name":        "Kisan Credit Card (KCC)",
            "benefit":     "Short-term crop loans at 4% effective interest per year",
            "eligibility": "Farmers, sharecroppers, and tenant farmers",
            "link":        "https://www.nabard.org/kcc",
            "min_age": 18, "max_age": 75, "gender": "all",
        },
        {
            "name":        "Soil Health Card Scheme",
            "benefit":     "Free soil testing + personalised fertiliser recommendation",
            "eligibility": "All farmers with agricultural land",
            "link":        "https://soilhealth.dac.gov.in",
            "min_age": 18, "max_age": 99, "gender": "all",
        },
        {
            "name":        "PM Krishi Sinchayee Yojana (PMKSY)",
            "benefit":     "Drip / sprinkler irrigation subsidies up to 55%",
            "eligibility": "Farmers with cultivable land",
            "link":        "https://pmksy.gov.in",
            "min_age": 18, "max_age": 99, "gender": "all",
        },
        {
            "name":        "National Agriculture Market (eNAM)",
            "benefit":     "Sell on an online mandi — best price, no middlemen",
            "eligibility": "All registered farmers",
            "link":        "https://www.enam.gov.in",
            "min_age": 18, "max_age": 99, "gender": "all",
        },
        {
            "name":        "Mahila Kisan Sashaktikaran Pariyojana (MKSP)",
            "benefit":     "Special training, equipment support, and financial aid for women farmers",
            "eligibility": "Women farmers only",
            "link":        "https://aajeevika.gov.in",
            "min_age": 18, "max_age": 60, "gender": "female",
        },
        {
            "name":        "PM Mudra Yojana — Agriculture",
            "benefit":     "Collateral-free loans Rs 50,000 – Rs 10 lakh for agri-allied activities",
            "eligibility": "Farmers under 50 for agri-enterprise",
            "link":        "https://www.mudra.org.in",
            "min_age": 18, "max_age": 50, "gender": "all",
        },
        {
            "name":        "PM-AASHA (Annadata Aay Sanrakshan Abhiyan)",
            "benefit":     "Government buys crops at guaranteed Minimum Support Price (MSP)",
            "eligibility": "Farmers growing MSP-notified crops",
            "link":        "https://agricoop.nic.in",
            "min_age": 18, "max_age": 99, "gender": "all",
        },
        {
            "name":        "AgriStack — Unified Farmer Service Interface",
            "benefit":     "One digital farmer ID to access all government benefits",
            "eligibility": "Tech-aware young farmers (18–40)",
            "link":        "https://agristack.gov.in",
            "min_age": 18, "max_age": 40, "gender": "all",
        },
        {
            "name":        "Senior Farmer Pension — Maharashtra (MAHADBT)",
            "benefit":     "Monthly pension support for elderly farmers unable to work",
            "eligibility": "Maharashtra farmers above 60 years",
            "link":        "https://mahadbt.maharashtra.gov.in",
            "min_age": 60, "max_age": 99, "gender": "all",
        },
        {
            "name":        "PM Kisan Maandhan Yojana (PM-KMY)",
            "benefit":     "Monthly pension of Rs 3,000 after age 60 with small contributions",
            "eligibility": "Small and marginal farmers aged 18–40",
            "link":        "https://pmkmy.gov.in",
            "min_age": 18, "max_age": 40, "gender": "all",
        },
    ]

    qualified = [
        s for s in all_schemes
        if (s["min_age"] <= user_age <= s["max_age"] if user_age else True)
        and (s["gender"] == "all" or s["gender"] == user_gender)
    ]

    return jsonify({
        "schemes":     qualified,
        "user_age":    user_age,
        "user_gender": user_gender,
        "total":       len(qualified),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
