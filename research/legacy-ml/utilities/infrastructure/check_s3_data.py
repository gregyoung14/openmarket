"""Check what's available on S3."""
import os

import boto3

# Credentials must be supplied via environment variables (AWS_ACCESS_KEY_ID,
# AWS_SECRET_ACCESS_KEY) or the default AWS credential chain. They are
# intentionally not hardcoded here.
S3_REGION = os.environ.get('AWS_DEFAULT_REGION', 'eu-west-1')
BUCKET_NAME = os.environ.get('S3_BUCKET_NAME', 'polymarket-btc-scraper-data-2026')

s3 = boto3.client('s3', region_name=S3_REGION)

paginator = s3.get_paginator('list_objects_v2')
objects = []

for page in paginator.paginate(Bucket=BUCKET_NAME):
    for obj in page.get('Contents', []):
        objects.append(obj)

objects.sort(key=lambda x: x['LastModified'], reverse=True)

print(f"Total: {len(objects)} objects\n")
for obj in objects:
    size_mb = obj['Size'] / (1024 * 1024)
    print(f"  {size_mb:8.2f} MB  {obj['LastModified'].strftime('%Y-%m-%d %H:%M')}  {obj['Key']}")
