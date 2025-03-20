# ABCD REDCap2ReproSchema Conversion Workflow

This repository contains a GitHub Actions workflow and Python script for automatically converting ABCD RedCap data dictionaries to ReproSchema format. The conversion runs on a monthly schedule and can also be triggered manually.

## Overview

The system periodically checks an S3 bucket for new RedCap data dictionary CSV files, processes them through the `reproschema` tool, and commits the converted results to this repository. It uses version tracking to only process new or updated files.

## Components

1. **GitHub Actions Workflow** (`.github/workflows/redcap2rs.yml`): Runs monthly and handles the conversion process
2. **Conversion Script** (`scripts/converter.py`): Python script that manages file retrieval, conversion, and git operations
3. **Configuration** (`config/config.yaml`): YAML file containing configuration parameters

## Setup and Configuration

### AWS Credentials

The workflow requires AWS credentials to access the S3 bucket containing RedCap data dictionaries. These credentials are stored as GitHub repository secrets:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

### Configuration File

The `config/config.yaml` file contains various settings:

```yaml
aws:
  bucket_name: "your-bucket-name"
  prefix: "your-prefix"
  endpoint_url: "your-endpoint-url"
  region: "your-region"
  # Credentials are pulled from environment variables

git:
  repo_path: "."  # Path to the repository

protocol:
  name: "your-protocol-name"
  yaml_path: "path/to/your/config.yaml"

processing:
  max_files: 1  # Process at most 1 file per run
  validate: true  # Validate schema after conversion

archive_processed_files: false  # Whether to move processed files to an archive folder
```

## How It Works

1. **Scheduling**: The workflow runs on the first day of each month at 1:00 AM UTC
2. **Manual Triggering**: Can be manually triggered from the GitHub Actions tab
3. **File Processing**:
   - Lists CSV files in the configured S3 bucket
   - Identifies files with newer versions than previously processed
   - Downloads candidate files for processing
   - Updates the protocol YAML with the current version
   - Converts files using the `reproschema` tool
   - Validates the generated schema
   - Updates the repository with new/modified files
   - Commits and tags changes
   - Optionally archives processed files in S3

4. **Version Management**:
   - Tracks versions through git tags and schema version fields
   - Only processes files with newer versions than already in the repository
   - Handles version-only changes intelligently

5. **Error Handling**:
   - Implements retry logic for AWS operations
   - Continues processing if individual files fail
   - Logs errors and progress information

## Logging

Logs are output to the console and visible in GitHub Actions run logs. The logs include detailed information about:
- Files discovered and their versions
- Processing steps and outcomes
- Error messages for debugging

## Manually Triggering the Workflow

To manually trigger the conversion:

1. Go to the "Actions" tab in the repository
2. Select "ReproSchema Conversion" from the workflows list
3. Click "Run workflow" button
4. Select the branch (usually "main")
5. Click the green "Run workflow" button

## Dependencies

The following dependencies are required:
- Python 3.10+
- reproschema
- boto3
- pyyaml
- gitpython

Dependencies are automatically installed by the GitHub Actions workflow.

## Troubleshooting

Common issues:

1. **AWS Credential Errors**: Check that the GitHub repository secrets are correctly set
2. **Conversion Errors**: May indicate issues with the data dictionary format or reproschema tool
3. **Git Errors**: Could be related to permissions or conflicts in the repository

Check the GitHub Actions logs for detailed error messages and information.