"""Check what's available on S3."""
import boto3

S3_CONFIG = {
    'aws_access_key_id': 'AKIAQLBX53LSSJANLUQE',
    'aws_secret_access_key': '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',
    'region_name': 'eu-west-1'
}
BUCKET_NAME = 'polymarket-btc-scraper-data-2026'

s3 = boto3.client('s3', **S3_CONFIG)

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
