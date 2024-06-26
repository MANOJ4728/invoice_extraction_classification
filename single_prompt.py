from flask import Flask, render_template, request, jsonify
import boto3
from pymongo import MongoClient
import requests
import json
import os
from dotenv import load_dotenv
import pprint

pp = pprint.PrettyPrinter(indent=2)

app = Flask(__name__)

load_dotenv()
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.environ.get("AWS_REGION")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME")]
flag=0

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
)
textract_client = boto3.client(
    "textract",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
)

with open("keywords.json", "r") as json_file:
    keywords = json.loads(json_file.read())

regular_bill_format = {
    "invoice_number": "",
    "invoice_date": "bill_date_value or issued_date_value",
    "invoice_amount": "invoice_amount_value",
    "payment_due_date": "",
    "vendor_name": "",
    "vendor_address": "",
    "customer_number": "",
    "page_numbers": [],
    "all_addresses": ['vendor_address','customer_address'],
    "Taxes":"",
    "discounts": "",
    "payments/adjustments/credits": "",
    "delivery_charges/Freight charges": "",
    "line_items": ["description","quantity","unit_price","net_amount","purposes"],
}
   

utility_bill_format = {
    "invoice_number":"",
    "invoice_date": "bill_date_vlaue or issued_date_value",
    "invoice_amount": "invoice_amount_value",
    "payment_due_date": "",
    "vendor_name": "",
    "vendor_address": "",
    # "contract_number": "",
    "account_number": "",
    # "customer_number": "",
    "page_numbers": [],
    "all_addresses": ['vendor_address','customer_address'],
    "Taxes": "total_tax_amount_value",
    "discounts": "total_discounts_amount_value",
    "payments/adjustments/credits": "",
    "previous_month_balance": "",
    "current_month_charges": ["current_month_charges_amount_value"],
    "line_items": ["description","current_charges","purposes"],
    "delivery_charges": "",
    "total_amount_due": "",
    "description": [],
    "amount": []
}

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["pdf"]
    filename = file.filename

    isAlreadyExists = db["extracted_text"].find_one({"filename": filename})
    ocr_blocks = None

    if isAlreadyExists is None:
        s3_client.upload_fileobj(file, S3_BUCKET_NAME, filename)

        ocr_response = textract_client.start_document_text_detection(
            DocumentLocation={"S3Object": {"Bucket": S3_BUCKET_NAME, "Name": filename}}
        )

        while True:
            ocr_text = textract_client.get_document_text_detection(
                JobId=ocr_response["JobId"]
            )
            status = ocr_text["JobStatus"]
            if status in ["SUCCEEDED", "FAILED"]:
                break

        if status == "SUCCEEDED":
            db["extracted_text"].insert_one({"filename": filename, "text": ocr_text})
            ocr_blocks = ocr_text["Blocks"]
        else:
            return jsonify({"error": "OCR failed"}), 500

    else:
        ocr_blocks = isAlreadyExists["text"]["Blocks"]

    extracted_text = ""
    for item in ocr_blocks:
        if item["BlockType"] == "LINE":
            extracted_text += item["Text"] + "\n"
    print("Extracted Text:", extracted_text)

    gpt_response = gpt_function(extracted_text)
    if gpt_response:
        print(type(gpt_response))
        collection_name = "temporary_collection"
        db[collection_name].insert_one({"gpt_response": gpt_response, "filename": filename})
        print(type(gpt_response))

        return render_template(
            "upload_success.html", gpt_response=gpt_response
        )
    else:
        flag=flag+1
        if flag<3:
            print("GPT processing failed trying again by sending to gpt server")
            gpt_function(extracted_text)
        else:    
            return jsonify({"error": "GPT processing failed"}), 500

def gpt_function(extracted_text):
    prompt=(
        f"You are a meticulous accountant processing an invoice. Carefully examine the text extracted from PDF files: '{extracted_text}'.\n"
        f"Examine the extracted text and classify it whether it is utility bill or regular bill\n"
        f"If it is utility bill it provides following services {keywords['type_of_utility']} and the entities like {keywords['utility_entites']}"
        f"and mainly every utility bill contains account number and examine correctly if it satisfies following constraints mentioned consider it as utility bill"
        f"If it is a utility bill, extract the following details only in the required format,follow this format strictly dont give any extra word other than i asked: {utility_bill_format}.\n" 
        f"If it is a regular bill,contains the following details like invoice number,date,amount and line items\n"
        f"If it is a regular bill, extract the following details only in the required format,follow this format strictly dont give any extra word other than i asked:: {regular_bill_format}.\n"
        f"Do not include any extra content or words or headings like Regular bill: etc or like these keywords on top of the gpt response just give the response in mentioned format only follow this strictly."
        f"For regular bills, consider each description from the list and map it to the respective primary services only from the list given {keywords['primary_Services']}\,don't give purposes that are not present in the given list"
        f"Each description should contain only one primary service from the list given the primary service should be anyone of those from the list dont give outside from the list.\n"
        f"Dont give purposes for the descriptions that are not present in the given list."
        f"For regular bills,vendor_Address will be the 'remit to' addresss and dont consider 'bill to' as vendor address"
        f"For regular bills,please consider total tax amount and discounts if present in the text consider the last page invoice total as final amount"
        f"Avoid considering line items where their description matches related to taxes, shipping charges, freight charges, delivery charges, etc., mentioned in this list: {keywords['notlineitems']}.\n"
        f"If the quantity or number of items purchased or price is zero, also consider it as a line item.\n"
        f"Don't miss any line items. Carefully examine the text and extract the full number of line items.\n"
        f"Please follow the instructions carefully and avoid extracting irrelevant lines. Do not include any formatting other than what is asked.\n" 
        f"Strictly follow the format. Do not include any text other than the required format."      
    )
    key = os.environ.get("OPENAI_KEY")
    response = requests.post(
        url="https://api.openai.com/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        json={
            "model": "gpt-3.5-turbo",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful assistant and accountant.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        },
    )

    status = response.status_code

    if status == 200:
        json = response.json()
        choices: dict = json.get("choices", [None])[0]
        message: dict = choices.get("message") if choices else None
        content: str = message.get("content") if message else None
        print(type(content))
        if content:
            try:
                return eval(content)
            except Exception as e:
                pp.pprint(json)
                print("Error while processing gpt content", e)
    else:
        print("Error occur in ChatGPT", response.json(), status)
    return None
if __name__ == "__main__":
    app.run(debug=True)
