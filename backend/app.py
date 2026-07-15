from flask import Flask, render_template, request, redirect, session, url_for, flash, send_from_directory
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import os
import json
from werkzeug.utils import secure_filename
from google import genai
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io



from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from flask import Flask, render_template, jsonify, send_file
from flask_cors import CORS
app = Flask(__name__)
CORS(app)


# --- GOOGLE DRIVE API INITIALIZATION ---
# 1. Define the permissions we need (uploading files)
#SCOPES = ['https://www.googleapis.com/auth/drive.file']
# --- GOOGLE DRIVE API INITIALIZATION ---
# Update this line to allow permission sharing modifications
SCOPES = ['https://www.googleapis.com/auth/drive']

# 2. Authenticate using your existing Firebase JSON key
drive_creds = service_account.Credentials.from_service_account_file(
    'serviceAccountKey.json', scopes=SCOPES)

# 3. Build the Drive connection
drive_service = build('drive', 'v3', credentials=drive_creds)

# 4. PASTE YOUR FOLDER ID HERE
DRIVE_PARENT_FOLDER_ID = os.getenv("FOLDER_ID")


def upload_to_gdrive(file_object, case_number, folder_category):
    """
    Uploads a file to a specific case folder inside Google Drive and returns a shareable link.
    """
    try:
        # 1. Find or create the specific folder for this case (e.g., "Case_101")
        case_folder_id = get_or_create_case_folder(case_number)
        
        # 2. Format the file name cleanly (e.g., "pleadings_petition.pdf")
        safe_name = secure_filename(file_object.filename)
        drive_file_name = f"{folder_category}_{safe_name}"
        
        # 3. Tell Google Drive to put the file INSIDE the specific case folder
        file_metadata = {
            'name': drive_file_name,
            'parents': [case_folder_id] 
        }
        
        # 4. Process the upload
        media = MediaIoBaseUpload(
            io.BytesIO(file_object.read()), 
            mimetype=file_object.content_type, 
            resumable=True
        )
        
        uploaded_file = drive_service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id, webViewLink'
        ).execute()
        
        file_id = uploaded_file.get('id')
        print(f"✅ Successfully uploaded {drive_file_name} to Case_{case_number} folder.")
        
        # --- NEW ADDITION: GRANT 'ANYONE WITH LINK' READ PERMISSION ---
        user_permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        drive_service.permissions().create(
            fileId=file_id,
            body=user_permission
        ).execute()
        print(f"🔗 Shared access permissions updated for file ID: {file_id}")
        # -------------------------------------------------------------
        
        # Return the direct link
        return uploaded_file.get('webViewLink')
        
    except Exception as e:
        print(f"❌ Google Drive Upload Failed: {e}")
        return None



def get_or_create_case_folder(case_number):
    """
    Checks if a folder like 'Case_101' exists in the main Drive folder. 
    Creates it if it doesn't exist, and returns the Folder ID.
    """
    folder_name = f"Case_{case_number}"
    
    # Search Google Drive for this specific folder name inside your root folder
    query = f"name='{folder_name}' and '{DRIVE_PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    
    response = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    
    if files:
        # The folder already exists, return its ID
        return files[0].get('id')
    else:
        # The folder does not exist, so let's create it
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [DRIVE_PARENT_FOLDER_ID]
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        print(f"📁 Created new folder for {folder_name}")
        return folder.get('id')

def send_real_invoice_email(target_email, client_name, case_number, case_details, financials, expenses):
    """
    Sends a premium Zomato/Swiggy-style itemized legal invoice 
    using Gmail's SMTP server and standard email libraries.
    """
    sender_email = os.getenv("EMAIL_SENDER")
    app_password = os.getenv("EMAIL_PASSWORD")
    
    if not sender_email or not app_password:
        print("❌ SMTP Credentials missing in environment variables.")
        return False

    # 1. Financial Computations
    agreed_fee = float(financials.get('fees_amount', 0.0))
    fee_particulars = financials.get('particulars', 'Legal Consultation & Representation Services')
    
    total_expenses = 0.0
    expense_rows_html = ""
    
    # Building dynamic table rows for expenses
    for idx, exp in enumerate(expenses, 1):
        amt = float(exp.get('amount', 0.0))
        total_expenses += amt
        expense_rows_html += f"""
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 12px 0; vertical-align: top;">
                <span style="color: #ef4444; font-weight: bold; font-size: 10px; display: block; margin-bottom: 2px;">OUTLAY #{idx}</span>
                <strong style="color: #334155; display: block; font-size: 13px;">{exp.get('particulars', 'Court Outlay')}</strong>
                <span style="font-size: 11px; color: #94a3b8;">Logged: {exp.get('date', 'N/A')}</span>
            </td>
            <td align="right" style="padding: 12px 0; color: #475569; font-size: 13px; font-weight: 600;">₹{amt:,.2f}</td>
        </tr>
        """
        
    grand_total = agreed_fee + total_expenses
    current_date = datetime.now().strftime('%d %b, %Y')

    # 2. MIMEMultipart Configuration
    msg = MIMEMultipart('alternative')
    msg['From'] = f"LegalMatrix Billing <{sender_email}>"
    msg['To'] = target_email
    msg['Subject'] = f"📑 Invoice Statement #INV-{case_number} - {case_details.get('case_name', 'Case Update')}"

    # 3. Zomato/Swiggy-inspired Responsive HTML Structure
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Your LegalMatrix Invoice</title>
    </head>
    <body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; background-color: #ffffff; margin: 30px auto; border-radius: 16px; box-shadow: 0 10px 25px rgba(15, 23, 42, 0.08); overflow: hidden; border: 1px solid #e2e8f0;">
            
            <!-- Header (Premium Slate Gradient) -->
            <tr>
                <td style="background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); padding: 35px 40px; text-align: center;">
                    <h1 style="color: #fbbf24; margin: 0; font-size: 26px; font-weight: 800; letter-spacing: 1px;">⚖️ LEGALMATRIX</h1>
                    <p style="color: #94a3b8; margin: 6px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px;">Automated Case Ledger System</p>
                </td>
            </tr>

            <!-- Meta Details & Welcome -->
            <tr>
                <td style="padding: 35px 40px; border-bottom: 1px dashed #e2e8f0;">
                    <h2 style="margin: 0 0 12px 0; font-size: 20px; color: #0f172a; font-weight: 700;">Hello {client_name},</h2>
                    <p style="margin: 0; font-size: 14px; color: #64748b; line-height: 1.6;">We have compiled the ledger account summary details for your ongoing legal matter referenced under case index <strong style="color: #0f172a; font-family: monospace;">#{case_number}</strong>.</p>
                    
                    <table width="100%" style="margin-top: 25px; font-size: 12px; color: #64748b; border-top: 1px solid #f1f5f9; padding-top: 15px;">
                        <tr>
                            <td style="padding-bottom: 6px;"><strong>Case Title:</strong> {case_details.get('case_name', 'N/A')}</td>
                            <td align="right" style="padding-bottom: 6px;"><strong>Court Authority:</strong> {case_details.get('court_name', 'N/A')}</td>
                        </tr>
                        <tr>
                            <td><strong>Billing Cycle:</strong> Active</td>
                            <td align="right"><strong>Generated On:</strong> {current_date}</td>
                        </tr>
                    </table>
                </td>
            </tr>

            <!-- Invoice Particulars Breakdown -->
            <tr>
                <td style="padding: 35px 40px;">
                    <p style="margin: 0 0 15px 0; font-size: 11px; font-weight: bold; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px;">Itemized Invoice Statement</p>
                    
                    <table width="100%" style="border-collapse: collapse; font-size: 14px; text-align: left;">
                        <!-- Table Header -->
                        <tr style="border-bottom: 2px solid #e2e8f0; color: #475569; font-size: 12px; text-transform: uppercase;">
                            <th style="padding-bottom: 10px; font-weight: 700;">Particulars Description</th>
                            <th align="right" style="padding-bottom: 10px; font-weight: 700;">Amount</th>
                        </tr>

                        <!-- Professional Fee Item -->
                        <tr style="border-bottom: 1px solid #f1f5f9;">
                            <td style="padding: 16px 0; vertical-align: top;">
                                <strong style="color: #0f172a; display: block; font-size: 14px;">Professional Retainer Fee</strong>
                                <span style="font-size: 12px; color: #64748b; line-height: 1.4; display: block; margin-top: 2px;">{fee_particulars}</span>
                            </td>
                            <td align="right" style="padding: 16px 0; color: #0f172a; font-weight: 700; font-size: 14px;">₹{agreed_fee:,.2f}</td>
                        </tr>

                        <!-- Insert Dynamic Outlay Rows here -->
                        {expense_rows_html}
                    </table>

                    <!-- Calculation Totals Box (Swiggy Style Minimalist summary) -->
                    <table width="100%" style="margin-top: 30px; border-top: 2px solid #e2e8f0; padding-top: 15px; font-size: 14px;">
                        <tr>
                            <td style="color: #64748b; padding: 4px 0;">Subtotal (Professional Retainer)</td>
                            <td align="right" style="color: #0f172a; padding: 4px 0;">₹{agreed_fee:,.2f}</td>
                        </tr>
                        <tr>
                            <td style="color: #64748b; padding: 4px 0;">Disbursed Court Outlays</td>
                            <td align="right" style="color: #ef4444; padding: 4px 0; font-weight: 600;">+ ₹{total_expenses:,.2f}</td>
                        </tr>
                        <tr style="font-size: 18px; font-weight: bold;">
                            <td style="color: #0f172a; padding: 18px 0 5px 0; border-top: 1px solid #f1f5f9; margin-top: 10px;">Grand Total</td>
                            <td align="right" style="color: #10b981; padding: 18px 0 5px 0; border-top: 1px solid #f1f5f9; margin-top: 10px;">₹{grand_total:,.2f}</td>
                        </tr>
                    </table>
                </td>
            </tr>

            <!-- Footer Notes -->
            <tr>
                <td style="background-color: #f8fafc; padding: 30px 40px; text-align: center; border-top: 1px solid #e2e8f0;">
                    <p style="margin: 0; font-size: 12px; color: #64748b; line-height: 1.6;">This receipt is generated automatically via the Secure LegalMatrix core ledger system. Under current framework rules, physical signatures are omitted.</p>
                    <p style="margin: 12px 0 0 0; font-size: 10px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px;">&copy; 2026 LegalMatrix Core Workspace Systems.</p>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """
    
    # Attach HTML payload
    msg.attach(MIMEText(html_content, 'html'))

    # 4. Dispatch Phase via SMTP
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()  # Upgrade connection to secure TLS
        server.login(sender_email, app_password)
        server.send_message(msg)
        server.quit()
        print(f"✅ Invoice successfully delivered to {target_email}")
        return True
    except Exception as e:
        print(f"❌ Standard Email Delivery Failed: {e}")
        return False
        return False
        return False



def send_real_email(target_email, client_name, alert_type):
    # Setup your credentials
    sender_email = os.getenv("EMAIL_SENDER")
    app_password = os.getenv("EMAIL_PASSWORD")
    
    # Format the message
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = target_email
    msg['Subject'] = f"Legal Update: {alert_type.replace('_', ' ').title()}"
    
    # The actual email text
    body = f"Dear {client_name},\n\nThis is an automated notification regarding your case: {alert_type.replace('_', ' ')}.\n\nRegards,\nYour Legal Team"
    msg.attach(MIMEText(body, 'plain'))

    try:
        # Connect to Gmail's SMTP server
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls() # Secure the connection
        server.login(sender_email, app_password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"❌ Email Delivery Failed: {e}")
        return False
# 1. Force load the environment variables from your .env file
load_dotenv()

# 2. Grab the key out of the environment manually
api_key = os.getenv("GEMINI_API_KEY")

# 3. Explicitly pass it right into the Gemini client
gemini_client = genai.Client(api_key=api_key)

# Define the exact JSON structure we want the AI to return
class KYCDetails(BaseModel):
    full_name: str = Field(description="The full name extracted from the identity document.")
    dob: str = Field(description="Date of Birth extracted from the document.")
    document_number: str = Field(description="The primary ID number (e.g., PAN, Passport). Mask the first 4 digits for security with 'X'.")

app = Flask(__name__, template_folder='templates')
app.secret_key = "super_secure_vault_secret_key_encryption"

# Initialize Firebase Admin SDK
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- AI KYC EXTRACTION ROUTE ---
@app.route('/api/extract-kyc', methods=['POST'])
def extract_kyc():
    if 'kyc_image' not in request.files:
        return {"error": "No file uploaded"}, 400
        
    file = request.files['kyc_image']
    if file.filename == '':
        return {"error": "No file selected"}, 400
        
    filename = secure_filename(file.filename)
    temp_folder = 'temp_uploads'
    os.makedirs(temp_folder, exist_ok=True)
    temp_path = os.path.join(temp_folder, filename)
    file.save(temp_path)
    
    try:
        uploaded_file = gemini_client.files.upload(file=temp_path)
        prompt = "Extract the identity details from this legal document. Return only the structured data."
        
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[uploaded_file, prompt],
            config={
                "response_mime_type": "application/json",
                "response_schema": KYCDetails
            }
        )
        
        os.remove(temp_path)
        extracted_data = json.loads(response.text)
        return extracted_data, 200
        
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return {"error": str(e)}, 500


# --- SECURITY PROTECTION MIDDLEWARE ---
@app.before_request
def intercept_unauthenticated_sessions():
    allowed_routes = ['index', 'register', 'serve_css']
    if request.endpoint not in allowed_routes and 'user' not in session:
        return redirect(url_for('login'))


# --- USER AUTHENTICATION CONTROLLERS ---
@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        user_ref = db.collection('users').document(username).get()
        if user_ref.exists:
            user_data = user_ref.to_dict()
            if user_data.get('password') == password:
                if user_data.get('account_status', 'approved') != 'approved':
                    flash("Access Denied: Your account is pending Master Admin approval.", "error")
                    return redirect(url_for('login'))
                
                session['user'] = username
                session['role'] = user_data.get('role', 'viewer')
                flash(f"Access Granted. Welcome back, {username}!", "success")
                return redirect(url_for('dashboard'))
        
        flash("Invalid identification credentials supplied.", "error")
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role = request.form.get('role', 'viewer')
        
        if not username or not password:
            flash("All authorization credentials fields required.", "error")
            return redirect(url_for('register'))
            
        user_check = db.collection('users').document(username).get()
        if user_check.exists:
            flash("Account identity key already exists inside the system ledger.", "error")
            return redirect(url_for('register'))
            
        existing_users = list(db.collection('users').limit(1).stream())
        if len(existing_users) == 0:
            account_status = 'approved'
            role = 'admin'
            flash("First user detected. Auto-provisioned as Approved Master Admin.", "success")
        else:
            account_status = 'pending'
            flash("Registration submitted successfully. Please wait for Master Admin approval.", "success")
            
        db.collection('users').document(username).set({
            'username': username,
            'password': password,
            'role': role,
            'account_status': account_status,
            'created_at': firestore.SERVER_TIMESTAMP
        })
        
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    flash("Session terminated securely.", "success")
    return redirect(url_for('login'))


# --- ADMINISTRATIVE USER CONTROLLERS ---
@app.route('/admin/add_user', methods=['POST'])
def admin_add_user():
    if session.get('role') != 'admin':
        flash("Unauthorized action.", "error")
        return redirect(url_for('dashboard'))
        
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    role = request.form.get('role', 'viewer')
    account_status = request.form.get('account_status', 'approved')
    
    if not username or not password:
        flash("Username and Password parameters are mandatory.", "error")
        return redirect(url_for('dashboard', view='users'))
        
    user_ref = db.collection('users').document(username).get()
    if user_ref.exists:
        flash(f"User identity matching '{username}' already exists.", "error")
        return redirect(url_for('dashboard', view='users'))
        
    db.collection('users').document(username).set({
        'username': username,
        'password': password,
        'role': role,
        'account_status': account_status,
        'created_at': firestore.SERVER_TIMESTAMP
    })
    flash(f"System Operator Profile '{username}' successfully compiled.", "success")
    return redirect(url_for('dashboard', view='users'))


@app.route('/approve_user/<username>', methods=['POST'])
def approve_user(username):
    if session.get('role') != 'admin':
        flash("Unauthorized action.", "error")
        return redirect(url_for('dashboard'))
        
    db.collection('users').document(username).update({'account_status': 'approved'})
    flash(f"User '{username}' has been granted operational access.", "success")
    return redirect(url_for('dashboard', view='users'))


@app.route('/reject_user/<username>', methods=['POST'])
def reject_user(username):
    if session.get('role') != 'admin':
        flash("Unauthorized action.", "error")
        return redirect(url_for('dashboard'))
        
    db.collection('users').document(username).delete()
    flash(f"User '{username}' registration request was rejected.", "success")
    return redirect(url_for('dashboard', view='users'))


@app.route('/style.css')
def serve_css():
    return send_from_directory('templates', 'style.css', mimetype='text/css')

# --- CLOUD STORAGE UPLOAD ROUTE ---
@app.route('/api/upload-case-file', methods=['POST'])
def upload_case_file():
    # Security check: Only admins and editors can upload files
    if session.get('role') not in ['admin', 'editor']:
        return {"error": "Unauthorized action"}, 403

    if 'file' not in request.files:
        return {"error": "No file attached"}, 400
        
    file = request.files['file']
    case_number = request.form.get('case_number')
    category = request.form.get('category') # e.g., 'pleadings', 'drafts', 'order_sheets'
    
    if file.filename == '' or not case_number or not category:
        return {"error": "Missing file, case number, or category"}, 400
        
    # Upload to Google Drive using your helper function
    file_url = upload_to_gdrive(file, case_number, category)
    
    if file_url:
        # Save the returned link to Firestore under the correct folder category
        case_ref = db.collection('cases').document(str(case_number))
        case_ref.update({
            f'cloud_storage_links.{category}': firestore.ArrayUnion([file_url])
        })
        return {"success": True, "url": file_url, "message": "File uploaded and saved to database."}, 200
    else:
        return {"error": "Failed to upload file to Google Drive"}, 500
        


@app.route('/api/email-invoice', methods=['POST'])
def email_invoice_route():
    data = request.get_json() or {}
    case_number = data.get('case_number')
    
    if not case_number:
        return jsonify({"success": False, "error": "Case reference is required."}), 400
        
    # 1. Fetch Case Data dynamically from Firestore
    case_doc = db.collection('cases').document(str(case_number)).get()
    
    if not case_doc.exists:
        return jsonify({"success": False, "error": "Case mapping query returned empty matrix."}), 404
    
    selected_case = case_doc.to_dict()

    # 2. Safe Parsing of Data Variables
    client_personal = selected_case.get('client_personal_details', {})
    target_email = client_personal.get('email_no')
    client_name = client_personal.get('client_name', 'Client')
    
    if not target_email:
        return jsonify({"success": False, "error": "Client does not have a registered email address."}), 400
        
    case_details = selected_case.get('case_details', {})
    financials = selected_case.get('financials', {})
    
    # Safely fetch your dynamic expense log tracker array entries
    expenses = selected_case.get('expense_ledger', [])

    # 3. Call SMTP Helper Function
    email_status = send_real_invoice_email(
        target_email=target_email,
        client_name=client_name,
        case_number=case_number,
        case_details=case_details,
        financials=financials,
        expenses=expenses
    )

    if email_status:
        return jsonify({"success": True, "recipient": target_email})
    else:
        return jsonify({"success": False, "error": "SMTP server transmission failed. Check log analytics."}), 500


# ==================== NEW: DOWNLOAD PDF INVOICE ROUTE ====================
@app.route('/download_invoice/<case_id>')
def download_invoice(case_id):
    # Fetch case document dynamically
    case_doc = db.collection('cases').document(str(case_id)).get()
    if not case_doc.exists:
        flash("Case registry file not found in system storage.", "error")
        return redirect(url_for('dashboard'))
        
    case_data = case_doc.to_dict()
    client_personal = case_data.get('client_personal_details', {})
    client_name = client_personal.get('client_name', 'Client')
    case_details = case_data.get('case_details', {})
    financials = case_data.get('financials', {})
    
    # Safely pull our dynamically logged outlays from the database
    expenses = case_data.get('expense_ledger', [])
    
    # Financial Computations
    agreed_fee = float(financials.get('fees_amount', 0.0))
    fee_particulars = financials.get('particulars', 'Legal Consultation & Representation Services')
    
    # Setup PDF Canvas utilizing safe standards
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=letter, 
        rightMargin=40, 
        leftMargin=40, 
        topMargin=40, 
        bottomMargin=40
    )
    story = []
    
    styles = getSampleStyleSheet()
    
    # Custom Typography Styles
    title_style = ParagraphStyle(
        'InvTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=22, textColor=colors.HexColor('#0f172a'), spaceAfter=5
    )
    subtitle_style = ParagraphStyle(
        'InvSub', parent=styles['Normal'], fontName='Helvetica', fontSize=9, textColor=colors.HexColor('#64748b'), spaceAfter=20
    )
    normal_style = ParagraphStyle(
        'InvNorm', parent=styles['Normal'], fontName='Helvetica', fontSize=10, textColor=colors.HexColor('#334155'), spaceBefore=3, spaceAfter=3
    )
    bold_style = ParagraphStyle(
        'InvBold', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#0f172a'), spaceBefore=3, spaceAfter=3
    )
    
    # 1. Invoice Header Section
    story.append(Paragraph("⚖️ LEGALMATRIX CORE WORKSPACE SYSTEMS", title_style))
    story.append(Paragraph(f"Invoice Reference: #INV-{case_id} | Statement Date: {datetime.now().strftime('%d %b, %Y')}", subtitle_style))
    story.append(Spacer(1, 10))
    
    # 2. Metadata Visual Grid Table (532 pt total printable width)
    meta_data = [
        [
            Paragraph(f"<b>Billed To:</b> {client_name}", normal_style), 
            Paragraph(f"<b>Case Track Title:</b> {case_details.get('case_name', 'N/A')}", normal_style)
        ],
        [
            Paragraph(f"<b>Client Email:</b> {client_personal.get('email_no', 'N/A')}", normal_style), 
            Paragraph(f"<b>Court Authority:</b> {case_details.get('court_name', 'N/A')}", normal_style)
        ],
        [
            Paragraph(f"<b>Client Mobile:</b> {client_personal.get('mobile_no', 'N/A')}", normal_style), 
            Paragraph(f"<b>Operational Status:</b> {case_details.get('status_of_case', 'N/A').upper()}", normal_style)
        ]
    ]
    meta_table = Table(meta_data, colWidths=[266, 266])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('PADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#f1f5f9')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 20))
    
    # 3. Itemized Invoice Breakdown Matrix
    story.append(Paragraph("<b>Itemized Statement Summary</b>", bold_style))
    story.append(Spacer(1, 5))
    
    invoice_table_data = [
        [
            Paragraph("<b>Particulars Description</b>", bold_style), 
            Paragraph("<b>Transaction Date</b>", bold_style), 
            Paragraph("<b>Amount</b>", bold_style)
        ]
    ]
    
    # Main Retainer Fee Entry
    invoice_table_data.append([
        Paragraph(f"<b>Professional Retainer Fee</b><br/><font color='#64748b' size='8'>{fee_particulars}</font>", normal_style),
        Paragraph(case_details.get('last_date_of_hearing', 'N/A') or 'N/A', normal_style),
        Paragraph(f"₹ {agreed_fee:,.2f}", normal_style)
    ])
    
    # Inject Dynamic Expense Outlays from the Tracker
    total_expenses = 0.0
    for exp in expenses:
        amt = float(exp.get('amount', 0.0))
        total_expenses += amt
        invoice_table_data.append([
            Paragraph(f"<b>{exp.get('particulars', 'Court Outlay')}</b><br/><font color='#ef4444' size='8'>Reimbursable Disbursed Outlay</font>", normal_style),
            Paragraph(exp.get('date', 'N/A'), normal_style),
            Paragraph(f"₹ {amt:,.2f}", normal_style)
        ])
        
    grand_total = agreed_fee + total_expenses
    
    # Summary Box Totals rows
    invoice_table_data.append([
        Paragraph("<b>Subtotal (Professional Retainer)</b>", normal_style), "", Paragraph(f"₹ {agreed_fee:,.2f}", normal_style)
    ])
    invoice_table_data.append([
        Paragraph("<b>Disbursed Outlays</b>", normal_style), "", Paragraph(f"₹ {total_expenses:,.2f}", normal_style)
    ])
    invoice_table_data.append([
        Paragraph("<b>Grand Total Due</b>", bold_style), "", Paragraph(f"<b>₹ {grand_total:,.2f}</b>", bold_style)
    ])
    
    # Constructing standard 532 printable point layout
    item_table = Table(invoice_table_data, colWidths=[332, 100, 100])
    item_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
        ('LINEBELOW', (0, 0), (-1, 0), 1.5, colors.HexColor('#cbd5e1')),
        ('PADDING', (0, 0), (-1, -1), 8),
        ('LINEBELOW', (0, 1), (-1, -4), 0.5, colors.HexColor('#e2e8f0')),
        ('LINEABOVE', (0, -3), (-1, -3), 1.5, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0, -3), (-1, -1), colors.HexColor('#f8fafc')),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    
    story.append(item_table)
    story.append(Spacer(1, 35))
    
    # Footer disclaimer notes
    footer_style = ParagraphStyle(
        'InvFoot', parent=styles['Normal'], fontName='Helvetica-Oblique', fontSize=8.5, textColor=colors.HexColor('#94a3b8'), alignment=1
    )
    story.append(Paragraph("This document is a certified, computer-generated digital record of account compiled via LegalMatrix Systems.<br/>As such, traditional hand-signed elements are omitted.", footer_style))
    
    # Build Document
    doc.build(story)
    buffer.seek(0)
    
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"Invoice_INV-{case_id}.pdf",
        mimetype='application/pdf'
    )
# --- MAIN CORE CMS ROUTER ---
@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    role = session.get('role', 'viewer')
    current_view = request.args.get('view', 'search')
    selected_case_id = request.args.get('case_id', None)
    
    if request.method == 'POST' and current_view == 'add':
        if role not in ['admin', 'editor']:
            flash("Operation Denied: Insufficient security clearance.", "error")
            return redirect(url_for('dashboard', view='search'))
            
        case_num = request.form.get('case_number', '').strip()
        if not case_num:
            flash("Unique Case Number identifier is foundational.", "error")
            return redirect(url_for('dashboard', view='add'))
            
        # Parse dynamic adversarial and opposing counsel arrays
        opp_names = request.form.getlist('opposing_party_name[]')
        opp_addresses = request.form.getlist('opposing_party_address[]')
        opp_mobiles = request.form.getlist('opposing_party_mobile[]')
        opp_emails = request.form.getlist('opposing_party_email[]')
        
        opposite_parties = []
        for i in range(len(opp_names)):
            if opp_names[i].strip():
                opposite_parties.append({
                    'name': opp_names[i].strip(),
                    'address': opp_addresses[i].strip() if i < len(opp_addresses) else '',
                    'mobile_no': opp_mobiles[i].strip() if i < len(opp_mobiles) else '',
                    'email': opp_emails[i].strip() if i < len(opp_emails) else ''
                })

        counsel_names = request.form.getlist('opposing_counsel_name[]')
        counsel_addresses = request.form.getlist('opposing_counsel_address[]')
        counsel_mobiles = request.form.getlist('opposing_counsel_mobile[]')
        counsel_emails = request.form.getlist('opposing_counsel_email[]')
        counsel_vaks = request.form.getlist('opposing_counsel_vakalatnama[]')
        
        opposite_counsels = []
        for i in range(len(counsel_names)):
            if counsel_names[i].strip():
                opposite_counsels.append({
                    'name': counsel_names[i].strip(),
                    'address': counsel_addresses[i].strip() if i < len(counsel_addresses) else '',
                    'mobile_no': counsel_mobiles[i].strip() if i < len(counsel_mobiles) else '',
                    'email': counsel_emails[i].strip() if i < len(counsel_emails) else '',
                    'vakalatnama_link': counsel_vaks[i].strip() if i < len(counsel_vaks) else ''
                })

        # Calculate initial finances & operating outlays
        exp_ledger = []
        exp_part = request.form.get('exp_particulars', '').strip()
        exp_d = request.form.get('exp_date', '').strip()
        try:
            exp_amt = float(request.form.get('exp_amount') or 0)
        except ValueError:
            exp_amt = 0.0
        if exp_part and exp_amt > 0:
            exp_ledger.append({
                'particulars': exp_part,
                'date': exp_d if exp_d else datetime.now().strftime("%Y-%m-%d"),
                'amount': exp_amt
            })

        proof_of_payments = []
        try:
            initial_paid = float(request.form.get('initial_fees_paid') or 0)
        except ValueError:
            initial_paid = 0.0
        if initial_paid > 0:
            proof_of_payments.append({
                'amount': initial_paid,
                'date': datetime.now().strftime("%Y-%m-%d"),
                'receipt_id': f"REC-{case_num}"
            })

        # Process civil specifics
        reliefs_raw = request.form.get('reliefs_sought', '')
        reliefs_list = [r.strip() for r in reliefs_raw.split(',') if r.strip()] if reliefs_raw else []
        try:
            quantum_val = float(request.form.get('quantum') or 0)
        except ValueError:
            quantum_val = 0.0

        case_payload = {
            'case_number': case_num,
            'case_classification': request.form.get('case_classification', 'civil'),
            'client_personal_details': {
                'client_name': request.form.get('client_name', '').strip(),
                'fathers_name': request.form.get('fathers_name', '').strip(),
                'address': request.form.get('client_address', '').strip(),
                'mobile_no': request.form.get('client_mobile', '').strip(),
                'email_no': request.form.get('client_email', '').strip(),
                'emergency_contact': {
                    'name': request.form.get('emergency_name', '').strip(),
                    'relation': request.form.get('emergency_relation', '').strip(),
                    'mobile_no': request.form.get('emergency_mobile', '').strip(),
                    'email': request.form.get('emergency_email', '').strip()
                }
            },
            'case_details': {
                'case_name': request.form.get('case_name', '').strip(),
                'court_name': request.form.get('court_name', '').strip(),
                'judge_name': request.form.get('judge_name', '').strip(),
                'court_no': request.form.get('court_no', '').strip(),
                'cause_list_link': request.form.get('cause_list_link', '').strip(),
                'vc_link': request.form.get('vc_link', '').strip(),
                'order_sheets_link': request.form.get('order_sheets_link', '').strip(),
                'last_date_of_hearing': request.form.get('last_date_of_hearing', '').strip(),
                'next_date_of_hearing': request.form.get('next_date_of_hearing', '').strip(),
                'item_no': request.form.get('item_no', '').strip(),
                'stage_of_matter': request.form.get('stage_of_matter', '').strip(),
                'status_of_case': request.form.get('status_of_case', 'pending'),
                'case_priority_flagging_color': request.form.get('case_priority_flagging_color', '#fbbf24'),
                'client_reminder_thresholds': ["48_hours", "24_hours"]
            },
            'opposite_parties': opposite_parties,
            'opposite_counsels': opposite_counsels,
            'adobe_features_status': {
                'scan_completed': True if request.form.get('adobe_scan') else False,
                'pdf_converted': True if request.form.get('adobe_pdf') else False,
                'ocr_processed': True if request.form.get('adobe_ocr') else False,
                'bookmarked': True if request.form.get('adobe_bookmark') else False,
                'esign_executed': True if request.form.get('adobe_esign') else False,
                'translator_used': True if request.form.get('adobe_translator') else False,
                'true_type_font_verified': True if request.form.get('adobe_truetype') else False
            },
            'criminal_specific': {
                'fir_number': request.form.get('fir_number', '').strip(),
                'police_station': request.form.get('police_station', '').strip(),
                'sections_of_law': request.form.get('sections_of_law', '').strip(),
                'remarks_additional_info': request.form.get('remarks_additional_info', '').strip()
            },
            'civil_specific': {
                'nature_of_claim': request.form.get('nature_of_claim', '').strip(),
                'suit_type': request.form.get('suit_type', '').strip(),
                'quantum': quantum_val,
                'reliefs_sought_multiple': reliefs_list
            },
            'financials': {
                'invoice_template_fed': True,
                'client_details_mode': request.form.get('invoice_mode', 'automatic'),
                'fees_amount': float(request.form.get('fees_amount') or 0),
                'particulars': request.form.get('financial_particulars', '').strip(),
                'esign_invoice_status': False,
                'invoice_pdf_share_link': '',
                'ledger_balance_agreed': float(request.form.get('fees_amount') or 0),
                'proof_of_payments_received': proof_of_payments
            },
            'expense_ledger': exp_ledger,
            'cloud_storage_links': {
                'pleadings': [],
                'drafts': [],
                'research': [],
                'order_sheets': [],
                'notes': []
            },
            'notes': [],
            'created_at': firestore.SERVER_TIMESTAMP
        }
        
        db.collection('cases').document(case_num).set(case_payload)
        flash(f"Record File {case_num} completely compiled and written successfully.", "success")
        return redirect(url_for('dashboard', view='search', case_id=case_num))

    cases_stream = db.collection('cases').stream()
    all_cases = [doc.to_dict() for doc in cases_stream]
    
    selected_case = None
    if selected_case_id:
        target_doc = db.collection('cases').document(selected_case_id).get()
        if target_doc.exists:
            selected_case = target_doc.to_dict()

    all_users = []
    if role == 'admin' and current_view == 'users':
        users_stream = db.collection('users').stream()
        all_users = [doc.to_dict() for doc in users_stream]

    upcoming_hearings = []
    current_date_string = datetime.now().strftime("%Y-%m-%d")
    for case in all_cases:
        next_hearing = case.get('case_details', {}).get('next_date_of_hearing')
        if next_hearing and next_hearing >= current_date_string:
            upcoming_hearings.append({
                'case_number': case.get('case_number'),
                'client_name': case.get('client_personal_details', {}).get('client_name'),
                'next_hearing_date': next_hearing,
                'email': case.get('client_personal_details', {}).get('email_no'),
                'primary_contact': case.get('client_personal_details', {}).get('mobile_no')
            })
    upcoming_hearings.sort(key=lambda item: item['next_hearing_date'])

    return render_template(
        'dashboard.html',
        role=role,
        username=session['user'],
        current_view=current_view,
        all_cases=all_cases,
        selected_case=selected_case,
        selected_case_id=selected_case_id,
        all_users=all_users,
        upcoming_hearings=upcoming_hearings
    )


# --- INLINE EDIT ENTRIES MUTATION HANDLER ---
# --- INLINE EDIT ENTRIES MUTATION HANDLER ---
@app.route('/update_case/<case_id>', methods=['POST'])
def update_case(case_id):
    if session.get('role') not in ['admin', 'editor']:
        flash("Action Blocked: Read-only profiles cannot update records.", "error")
        return redirect(url_for('dashboard', view='search', case_id=case_id))
        
    case_ref = db.collection('cases').document(case_id)
    if not case_ref.get().exists:
        flash("Target case matching identifier not discovered.", "error")
        return redirect(url_for('dashboard', view='search'))

    # 1. Parse Dynamic Structural Array: Opposite Parties
    opp_names = request.form.getlist('opposing_party_name[]')
    opp_addresses = request.form.getlist('opposing_party_address[]')
    opp_mobiles = request.form.getlist('opposing_party_mobile[]')
    opp_emails = request.form.getlist('opposing_party_email[]')

    opposite_parties_list = []
    for i in range(len(opp_names)):
        if opp_names[i].strip():  # Skip empty rows if they weren't filled out
            opposite_parties_list.append({
                'name': opp_names[i].strip(),
                'address': opp_addresses[i].strip() if i < len(opp_addresses) else '',
                'mobile_no': opp_mobiles[i].strip() if i < len(opp_mobiles) else '',
                'email': opp_emails[i].strip() if i < len(opp_emails) else ''
            })
            
    # 2. Parse Dynamic Structural Array: Opposing Counsel
    counsel_names = request.form.getlist('opposing_counsel_name[]')
    counsel_addresses = request.form.getlist('opposing_counsel_address[]')
    counsel_mobiles = request.form.getlist('opposing_counsel_mobile[]')
    counsel_emails = request.form.getlist('opposing_counsel_email[]')
    counsel_vakalats = request.form.getlist('opposing_counsel_vakalatnama[]')

    opposing_counsel_list = []
    for i in range(len(counsel_names)):
        if counsel_names[i].strip():  # Skip empty rows
            opposing_counsel_list.append({
                'name': counsel_names[i].strip(),
                'address': counsel_addresses[i].strip() if i < len(counsel_addresses) else '',
                'mobile_no': counsel_mobiles[i].strip() if i < len(counsel_mobiles) else '',
                'email': counsel_emails[i].strip() if i < len(counsel_emails) else '',
                'vakalatnama_link': counsel_vakalats[i].strip() if i < len(counsel_vakalats) else ''
            })

    # 3. Parse Comma-Separated Lists (For Reliefs)
    reliefs_raw = request.form.get('reliefs_sought', '')
    reliefs_list = [r.strip() for r in reliefs_raw.split(',') if r.strip()] if reliefs_raw else []
    
    # 4. Parse Numeric Inputs Safely
    try:
        quantum_val = float(request.form.get('quantum') or 0)
    except ValueError:
        quantum_val = 0.0

    try:
        fees_amt = float(request.form.get('fees_amount') or 0)
    except ValueError:
        fees_amt = 0.0

    # 5. Build Update Package
    updated_fields = {
        'client_personal_details.client_name': request.form.get('client_name', '').strip(),
        'client_personal_details.fathers_name': request.form.get('fathers_name', '').strip(),
        'client_personal_details.address': request.form.get('client_address', '').strip(),
        'client_personal_details.mobile_no': request.form.get('client_mobile', '').strip(),
        'client_personal_details.email_no': request.form.get('client_email', '').strip(),
        
        'client_personal_details.emergency_contact.name': request.form.get('emergency_name', '').strip(),
        'client_personal_details.emergency_contact.relation': request.form.get('emergency_relation', '').strip(),
        'client_personal_details.emergency_contact.mobile_no': request.form.get('emergency_mobile', '').strip(),
        'client_personal_details.emergency_contact.email': request.form.get('emergency_email', '').strip(),
        
        'case_details.case_name': request.form.get('case_name', '').strip(),
        'case_details.court_name': request.form.get('court_name', '').strip(),
        'case_details.judge_name': request.form.get('judge_name', '').strip(),
        'case_details.court_no': request.form.get('court_no', '').strip(),
        'case_details.cause_list_link': request.form.get('cause_list_link', '').strip(),
        'case_details.vc_link': request.form.get('vc_link', '').strip(),
        'case_details.order_sheets_link': request.form.get('order_sheets_link', '').strip(),
        'case_details.last_date_of_hearing': request.form.get('last_date_of_hearing', '').strip(),
        'case_details.next_date_of_hearing': request.form.get('next_date_of_hearing', '').strip(),
        'case_details.item_no': request.form.get('item_no', '').strip(),
        'case_details.stage_of_matter': request.form.get('stage_of_matter', '').strip(),
        'case_details.status_of_case': request.form.get('status_of_case', 'pending'),
        'case_details.case_priority_flagging_color': request.form.get('case_priority_flagging_color', '#fbbf24'),
        
        # --- FIXED TARGET MAPPINGS (ROOT LEVEL) ---
        'opposite_parties': opposite_parties_list,
        'opposite_counsels': opposing_counsel_list,
        
        'criminal_specific.fir_number': request.form.get('fir_number', '').strip(),
        'criminal_specific.police_station': request.form.get('police_station', '').strip(),
        'criminal_specific.sections_of_law': request.form.get('sections_of_law', '').strip(),
        'criminal_specific.remarks_additional_info': request.form.get('remarks_additional_info', '').strip(),
        
        'civil_specific.nature_of_claim': request.form.get('nature_of_claim', '').strip(),
        'civil_specific.suit_type': request.form.get('suit_type', '').strip(),
        'civil_specific.quantum': quantum_val,
        'civil_specific.reliefs_sought_multiple': reliefs_list,
        
        'financials.fees_amount': fees_amt,
        'financials.particulars': request.form.get('financial_particulars', '').strip()
    }
    
    # 6. Commit to Firestore & Flash Success
    case_ref.update(updated_fields)
    flash("Case matrix modifications successfully committed!", "success")
    return redirect(url_for('dashboard', view='search', case_id=case_id))


# --- MEMO TIMELINE ATTACHMENT ROUTE ---
@app.route('/add_note', methods=['POST'])
def add_note():
    if session.get('role') == 'viewer':
        flash("Action Blocked: Viewers cannot create timeline notes.", "error")
        return redirect(url_for('dashboard'))
        
    case_id = request.form.get('case_id')
    content = request.form.get('note_content', '').strip()
    
    if case_id and content:
        note_obj = {
            'content': content,
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        case_ref = db.collection('cases').document(case_id)
        case_ref.update({
            'notes': firestore.ArrayUnion([note_obj])
        })
        flash("Timeline updated.", "success")
    return redirect(url_for('dashboard', view='search', case_id=case_id))


# --- PURGE ROUTE ---
@app.route('/delete_case/<case_id>', methods=['POST'])
def delete_case(case_id):
    if session.get('role') != 'admin':
        flash("Operation Intercepted: Only System Admins can purge records.", "error")
        return redirect(url_for('dashboard', view='search', case_id=case_id))
        
    db.collection('cases').document(case_id).delete()
    flash(f"Case Profile Document {case_id} cleanly expunged from database layers.", "success")
    return redirect(url_for('dashboard', view='search'))


# --- GOOGLE CLOUD BASE API SERVICE FOR ALERTS ---
# --- GOOGLE CLOUD BASE API SERVICE FOR ALERTS ---
# --- GOOGLE CLOUD BASE API SERVICE FOR ALERTS ---
@app.route('/api/send-alert', methods=['POST'])
def send_alert():
    data = request.json
    
    # Extract the necessary data from the frontend request
    case_number = data.get('case_number')
    alert_type = data.get('alert_type')
    dispatch_method = data.get('dispatch_method') # 'email' or 'sms'

    # Fallbacks in case the frontend is still sending the old payload
    client_name = data.get('client_name', 'Client')
    destination = data.get('destination')

    print(f"\n--- 🚀 ALERT DISPATCH INITIATED ---")
    print(f"Case Number Received: {case_number}")
    print(f"Dispatch Method: {dispatch_method}")

    # If the frontend didn't provide a direct destination, look it up in Firestore
    if not destination and case_number:
        print(f"Attempting to fetch destination from Firestore for Case: {case_number}")
        case_ref = db.collection('cases').document(str(case_number)).get()
        
        if case_ref.exists:
            case_data = case_ref.to_dict()
            client_details = case_data.get('client_personal_details', {})
            client_name = client_details.get('client_name', client_name)
            
            # Map the exact keys from your JSON schema
            if dispatch_method == 'email':
                destination = client_details.get('email_no')
                print(f"✅ DB Fetch Success -> client_email (email_no): {destination}")
            elif dispatch_method == 'sms':
                destination = client_details.get('mobile_no')
                print(f"✅ DB Fetch Success -> client_mobile (mobile_no): {destination}")
        else:
            print(f"❌ DB Fetch Failed -> Case document {case_number} does not exist.")

    # Failsafe if we still don't have a destination
    if not destination:
        print("❌ Dispatch Aborted -> Destination (email/mobile) is empty or None.")
        return {"success": False, "message": f"No contact information found for {dispatch_method}."}, 400

    # --- DISPATCH LOGIC ---
    if dispatch_method == 'email':
        print(f"Attempting to send real email to: {destination}")
        success = send_real_email(destination, client_name, alert_type)
        
        if success:
            print("✅ Email sent successfully via SMTP.")
            return {"success": True, "message": f"Email dispatched successfully to {destination}"}
        else:
            print("❌ Email sending failed inside send_real_email function.")
            return {"success": False, "message": "Backend error: Could not send email"}, 500

    elif dispatch_method == 'sms':
        print(f"[SIMULATION] SMS sent to {destination}")
        return {"success": True, "message": f"SMS simulated successfully to {destination}"}

    return {"success": False, "message": "Invalid dispatch method"}, 400


# --- RESEARCH MODULE ROUTE ---
@app.route('/case-research/<case_id>')
def case_research(case_id):
    case_doc = db.collection('cases').document(case_id).get()
    if not case_doc.exists:
        flash("Target case matching identifier not discovered for research mapping.", "error")
        return redirect(url_for('dashboard'))
        
    case_data = case_doc.to_dict()
    return render_template(
        'research.html', 
        case=case_data, 
        user_name=session.get('user'), 
        role=session.get('role')
    )


# --- GEMINI CASE SUMMARY GENERATION API ---
@app.route('/api/case-summary/<case_id>', methods=['POST'])
def api_case_summary(case_id):
    case_doc = db.collection('cases').document(case_id).get()
    if not case_doc.exists:
        return {"error": "Case data block not found in active node."}, 404
        
    case_data = case_doc.to_dict()
    case_details_str = json.dumps(case_data, default=str)
    
    prompt = f"Analyze this legal case data and provide a comprehensive strategic legal summary, risk analysis, and next steps brief:\n{case_details_str}"
    
    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return {"summary": response.text}, 200
    except Exception as e:
        return {"error": str(e)}, 500


# --- INDIAN KANOON PRECEDENT SEARCH API MOCK ---
@app.route('/api/indian-kanoon-search', methods=['POST'])
def api_indian_kanoon_search():
    data = request.get_json() or {}
    query = data.get('query', '').strip()
    if not query:
        return {"error": "Search query parameter missing."}, 400
        
    mock_results = [
        {
            "title": f"State vs. Litigant Pool - Core Interpretation of {query}",
            "context": f"In this matter, the Court analyzed the procedural compliance and identity documentation workflows of {query}.",
            "docid": "1094852",
            "publishdate": "2023-08-14"
        },
        {
            "title": f"John Doe vs. Union of India ({query} precedent reference)",
            "context": f"A landmark judgment reviewing structural identity verifications and corporate escrow standards under {query}.",
            "docid": "2384910",
            "publishdate": "2021-11-02"
        }
    ]
    return {"results": mock_results, "note": "Precedent directory search compiled from available legal nodes."}, 200

# --- ADD EXPENSE TO LEDGER ROUTE ---
@app.route('/add_expense/<case_id>', methods=['POST'])
def add_expense(case_id):
    if session.get('role') == 'viewer':
        flash("Action Blocked: Viewers cannot add expenses.", "error")
        return redirect(url_for('dashboard', view='search', case_id=case_id))
        
    particulars = request.form.get('exp_particulars', '').strip()
    date_val = request.form.get('exp_date', '').strip()
    try:
        amount = float(request.form.get('exp_amount') or 0)
    except ValueError:
        amount = 0.0

    if particulars and amount > 0:
        expense_obj = {
            'particulars': particulars,
            'date': date_val if date_val else datetime.now().strftime("%Y-%m-%d"),
            'amount': amount
        }
        
        # Append to the case's expense ledger array in Firestore
        case_ref = db.collection('cases').document(case_id)
        case_ref.update({
            'expense_ledger': firestore.ArrayUnion([expense_obj])
        })
        flash("Expense added to ledger successfully.", "success")
    else:
        flash("Invalid expense details supplied.", "error")
        
    return redirect(url_for('dashboard', view='search', case_id=case_id))
if __name__ == '__main__':
    app.run(debug=True, port=5000)
