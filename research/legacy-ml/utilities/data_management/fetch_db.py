import sqlite3
import boto3
import os

# AWS S3 credentials and bucket name
# Note: These were provided in the user request.
S3_CONFIG = {
    'aws_access_key_id': 'AKIAQLBX53LSSJANLUQE',
    'aws_secret_access_key': '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',
    'region_name': 'eu-west-1'
}
BUCKET_NAME = 'polymarket-btc-scraper-data-2026'
DB_FILE_NAME = 'polymarket_btc_data.db'
S3_KEY = 'raw/polymarket_btc_data.db'

def cleanup_local_db():
    print(f"Cleaning up existing database files ({DB_FILE_NAME}, {DB_FILE_NAME}-wal, {DB_FILE_NAME}-shm)...")
    for ext in ['', '-wal', '-shm']:
        file_path = f"{DB_FILE_NAME}{ext}"
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Removed {file_path}")
    print("Cleanup complete.")

def download_from_s3():
    print(f"Re-downloading {DB_FILE_NAME} from S3 bucket '{BUCKET_NAME}'...")
    s3 = boto3.client('s3', **S3_CONFIG)
    try:
        s3.download_file(BUCKET_NAME, S3_KEY, DB_FILE_NAME)
        print("Download complete.")

        if not os.path.exists(DB_FILE_NAME):
            raise FileNotFoundError(f"{DB_FILE_NAME} was not found after download.")
        
        file_size = os.path.getsize(DB_FILE_NAME)
        print(f"{DB_FILE_NAME} size: {file_size} bytes.")
        if file_size == 0:
            raise ValueError(f"{DB_FILE_NAME} is empty after download.")
            
    except Exception as e:
        print(f"Error downloading from S3: {e}")
        raise

def inspect_db():
    print("\nDatabase Schema:\n")
    try:
        conn = sqlite3.connect(DB_FILE_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()

        for table_name in tables:
            table_name = table_name[0]
            print(f"Table: {table_name}")
            cursor.execute(f"PRAGMA table_info({table_name});")
            columns = cursor.fetchall()
            for col in columns:
                print(f"  Column Name: {col[1]}, Data Type: {col[2]}, Not Null: {bool(col[3])}, Primary Key: {bool(col[5])})")
            print("\n")
        conn.close()
    except Exception as e:
        print(f"Error inspecting DB: {e}")

if __name__ == "__main__":
    cleanup_local_db()
    download_from_s3()
    inspect_db()
