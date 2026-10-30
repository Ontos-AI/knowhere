#!/bin/sh
set -eu

for bucket in knowhere-progressive-e2e-uploads knowhere-progressive-e2e-results; do
    if ! awslocal s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
        awslocal s3api create-bucket \
            --bucket "$bucket" \
            --region us-west-1 \
            --create-bucket-configuration LocationConstraint=us-west-1 >/dev/null
    fi
done

python /e2e/configure-storage-notifications.py \
    --phase configure --storage-endpoint http://localhost:4566
touch /tmp/progressive-e2e-storage-ready
