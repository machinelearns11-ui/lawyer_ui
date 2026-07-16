from flask import Flask, request, jsonify, send_file
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import os
import json
import io
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

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from flask_cors import CORS

# --- FORCE ENVIRONMENT LABELS ---
load_dotenv()

app = Flask(__name__)
# Enable CORS so your Static Frontend can communicate with this backend service
CORS(app, supports_credentials=True)

app.secret_key = os.getenv("FLASK_SECRET_KEY", "super_secure_fallback_key")

# --- INITIALIZE FIREBASE ADMIN SDK ---
# Automatically searches root directory for serviceAccountKey.json
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- GOOGLE DRIVE API INITIALIZATION ---
SCOPES = ['https://www.googleapis.com/auth/drive']
drive_creds = service_account.Credentials.from_service_account_file(
    'serviceAccountKey.json', scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=drive_creds)
DRIVE_PARENT_FOLDER_ID = os.getenv("FOLDER_ID")

# --- GOOGLE GENAI CONFIGURATION ---
api_key = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=api_key)

class KYCDetails(BaseModel):
    full_name: str = Field(description="The full name extracted from the identity document.")
    dob: str = Field(description="Date of Birth extracted from the document.")
    document_number: str = Field(description="The primary ID number (e.g., PAN, Passport). Mask the first 4 digits for security with 'X'.")

# --- HELPER UTILITIES ---
def get_or_create_case_folder(case_number):
    folder_name = f"Case_{case_number}"
    query = f"name='{folder_name}' and '{DRIVE_PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    response = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    
    if files:
        return files[0].get('id')
    else:
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [DRIVE_PARENT_FOLDER_ID]
        }
        folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')

def upload_to_gdrive(file_object, case_number, folder_category):
    try:
        case_folder_id = get_or_create_case_folder(case_number)
        safe_name = secure_filename(file_object.filename)
        drive_file_name = f"{folder_category}_{safe_name}"
        
        file_metadata = {
            'name': drive_file_name,
            'parents': [case_folder_id] 
        }
        
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
        
        user_permission = {'type': 'anyone', 'role': 'reader'}
        drive_service.permissions().create(fileId=file_id, body=user_permission).execute()
        
        return uploaded_file.get('webViewLink')
    except Exception as e:
        print(f"❌ Google Drive Upload Failed: {e}")
        return None

def send_real_invoice_email(target_email, client_name, case_number, case_details, financials, expenses):
    sender_email = os.getenv("EMAIL_SENDER")
    app_password = os.getenv("EMAIL_PASSWORD")
    
    if not sender_email or not app_password:
        return False

    agreed_fee = float(financials.get('fees_amount', 0.0))
    fee_particulars = financials.get('particulars', 'Legal Consultation Services')
    
    total_expenses = 0.0
    expense_rows_html = ""
    
    for idx, exp in enumerate(expenses, 1):
        amt = float(exp.get('amount', 0.0))
        total_expenses += amt
        expense_rows_html += f"""
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 12px 0; vertical-align: top;">
                <span style="color: #ef4444; font-weight: bold; font-size: 10px; display: block;">OUTLAY #{idx}</span>
                <strong style="color: #334155; display: block; font-size: 13px;">{exp.get('particulars', 'Court Outlay')}</strong>
            </td>
            <td align="right" style="padding: 12px 0; color: #475569; font-size: 13px;">₹{amt:,.2f}</td>
        </tr>
        """
        
    grand_total = agreed_fee + total_expenses
    current_date = datetime.now().strftime('%d %b, %Y')

    msg = MIMEMultipart('alternative')
    msg['From'] = f"LegalMatrix Billing <{sender_email}>"
    msg['To'] = target_email
    msg['Subject'] = f"📑 Invoice Statement #INV-{case_number}"

    html_content = f"""
    <html>
    <body style="background-color: #f8fafc; font-family: sans-serif; padding: 20px;">
        <div style="max-width: 600px; background: #fff; margin: auto; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
            <h2>Hello {client_name},</h2>
            <p>Invoice Summary for Case File: <strong>#{case_number}</strong></p>
            <table width="100%" style="border-collapse: collapse;">
                <tr style="border-bottom: 2px solid #e2e8f0;">
                    <th>Description</th>
                    <th align="right">Amount</th>
                </tr>
                <tr>
                    <td style="padding: 10px 0;">Professional Retainer ({fee_particulars})</td>
                    <td align="right">₹{agreed_fee:,.2f}</td>
                </tr>
                {expense_rows_html}
                <tr style="font-weight: bold; font-size: 16px;">
                    <td style="padding-top: 20px;">Grand Total</td>
                    <td align="right" style="padding-top: 20px; color: #10b981;">₹{grand_total:,.2f}</td>
                </tr>
            </table>
        </div>
    </body>
    </html>
    """
    msg.attach(MIMEText(html_content, 'html'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, app_password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"❌ Email Transmission Failed: {e}")
        return False

# --- LIVE API ENTRANCES ---

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    if not username or not password:
        return jsonify({"success": False, "error": "Missing validation fields."}), 400

    user_ref = db.collection('users').document(username).get()
    if user_ref.exists:
        user_data = user_ref.to_dict()
        if user_data.get('password') == password:
            if user_data.get('account_status', 'approved') != 'approved':
                return jsonify({"success": False, "error": "Access Denied: Pending Admin approval."}), 403
            
            return jsonify({
                "success": True, 
                "username": username,
                "role": user_data.get('role', 'viewer'),
                "message": "Access Granted."
            }), 200
            
    return jsonify({"success": False, "error": "Invalid verification credentials."}), 401


@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    role = data.get('role', 'viewer')
    
    if not username or not password:
        return jsonify({"success": False, "error": "All operational fields required."}), 400
        
    user_check = db.collection('users').document(username).get()
    if user_check.exists:
        return jsonify({"success": False, "error": "Identity key already present inside database."}), 400
        
    existing_users = list(db.collection('users').limit(1).stream())
    if len(existing_users) == 0:
        account_status = 'approved'
        role = 'admin'
    else:
        account_status = 'pending'
        
    db.collection('users').document(username).set({
        'username': username,
        'password': password,
        'role': role,
        'account_status': account_status,
        'created_at': firestore.SERVER_TIMESTAMP
    })
    
    return jsonify({"success": True, "status": account_status}), 200


@app.route('/api/extract-kyc', methods=['POST'])
def extract_kyc():
    if 'kyc_image' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files['kyc_image']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
        
    filename = secure_filename(file.filename)
    temp_folder = 'temp_uploads'
    os.makedirs(temp_folder, exist_ok=True)
    temp_path = os.path.join(temp_folder, filename)
    file.save(temp_path)
    
    try:
        uploaded_file = gemini_client.files.upload(file=temp_path)
        prompt = "Extract details from document. Return structured data."
        
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[uploaded_file, prompt],
            config={"response_mime_type": "application/json", "response_schema": KYCDetails}
        )
        
        os.remove(temp_path)
        return json.loads(response.text), 200
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return jsonify({"error": str(e)}), 500


@app.route('/api/upload-case-file', methods=['POST'])
def upload_case_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file attached"}), 400
        
    file = request.files['file']
    case_number = request.form.get('case_number')
    category = request.form.get('category')
    
    if file.filename == '' or not case_number or not category:
        return jsonify({"error": "Missing mandatory form components"}), 400
        
    file_url = upload_to_gdrive(file, case_number, category)
    
    if file_url:
        case_ref = db.collection('cases').document(str(case_number))
        case_ref.update({
            f'cloud_storage_links.{category}': firestore.ArrayUnion([file_url])
        })
        return jsonify({"success": True, "url": file_url}), 200
    return jsonify({"error": "Google Drive target upload failed"}), 500


@app.route('/api/email-invoice', methods=['POST'])
def email_invoice_route():
    data = request.get_json() or {}
    case_number = data.get('case_number')
    
    if not case_number:
        return jsonify({"success": False, "error": "Case reference required."}), 400
        
    case_doc = db.collection('cases').document(str(case_number)).get()
    if not case_doc.exists:
        return jsonify({"success": False, "error": "Case record not found."}), 404
        
    selected_case = case_doc.to_dict()
    client_personal = selected_case.get('client_personal_details', {})
    target_email = client_personal.get('email_no')
    client_name = client_personal.get('client_name', 'Client')
    
    if not target_email:
        return jsonify({"success": False, "error": "Missing client contact parameters."}), 400
        
    email_status = send_real_invoice_email(
        target_email=target_email,
        client_name=client_name,
        case_number=case_number,
        case_details=selected_case.get('case_details', {}),
        financials=selected_case.get('financials', {}),
        expenses=selected_case.get('expense_ledger', [])
    )

    if email_status:
        return jsonify({"success": True, "recipient": target_email}), 200
    return jsonify({"success": False, "error": "SMTP Gateway drop error."}), 500


@app.route('/api/download-invoice/<case_id>', methods=['GET'])
def download_invoice(case_id):
    case_doc = db.collection('cases').document(str(case_id)).get()
    if not case_doc.exists:
        return jsonify({"error": "Case tracking ID invalid."}), 404
        
    case_data = case_doc.to_dict()
    client_personal = case_data.get('client_personal_details', {})
    client_name = client_personal.get('client_name', 'Client')
    case_details = case_data.get('case_details', {})
    financials = case_data.get('financials', {})
    expenses = case_data.get('expense_ledger', [])
    
    agreed_fee = float(financials.get('fees_amount', 0.0))
    fee_particulars = financials.get('particulars', 'Legal Retainer Services')
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    story = []
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('InvTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=20, textColor=colors.HexColor('#0f172a'))
    normal_style = ParagraphStyle('InvNorm', parent=styles['Normal'], fontName='Helvetica', fontSize=10, textColor=colors.HexColor('#334155'))
    bold_style = ParagraphStyle('InvBold', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#0f172a'))
    
    story.append(Paragraph("⚖️ LEGALMATRIX LEDGER STATEMENT", title_style))
    story.append(Spacer(1, 15))
    
    invoice_table_data = [[Paragraph("<b>Description</b>", bold_style), Paragraph("<b>Date</b>", bold_style), Paragraph("<b>Amount</b>", bold_style)]]
    invoice_table_data.append([Paragraph(f"Retainer: {fee_particulars}", normal_style), Paragraph(case_details.get('last_date_of_hearing', 'N/A') or 'N/A', normal_style), Paragraph(f"₹ {agreed_fee:,.2f}", normal_style)])
    
    total_expenses = 0.0
    for exp in expenses:
        amt = float(exp.get('amount', 0.0))
        total_expenses += amt
        invoice_table_data.append([Paragraph(exp.get('particulars', 'Court Outlay'), normal_style), Paragraph(exp.get('date', 'N/A'), normal_style), Paragraph(f"₹ {amt:,.2f}", normal_style)])
        
    grand_total = agreed_fee + total_expenses
    invoice_table_data.append([Paragraph("<b>Grand Total</b>", bold_style), "", Paragraph(f"<b>₹ {grand_total:,.2f}</b>", bold_style)])
    
    item_table = Table(invoice_table_data, colWidths=[332, 100, 100])
    item_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f8fafc')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    
    story.append(item_table)
    doc.build(story)
    buffer.seek(0)
    
    return send_file(buffer, as_attachment=True, download_name=f"Invoice_{case_id}.pdf", mimetype='application/pdf')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
