import os
import shutil
import json
import tempfile
import datetime
import boto3
import yaml
from git import Repo, exc as git_exc
from subprocess import run, CalledProcessError, PIPE
import logging
import re
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv  

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('redcap2reproschema-ci')

load_dotenv()

class ReproSchemaConverter:
    def __init__(self, config_path='config.yaml'):
        """Initialize the converter with configurations"""
        try:
            with open(config_path, 'r') as f:
                self.config = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError) as e:
            logger.error(f"Failed to load config from {config_path}: {e}")
            sys.exit(1)
        
        # AWS configuration
        self.s3_bucket = self.config['aws']['bucket_name']
        self.s3_prefix = self.config['aws']['prefix']
        
        # Git repository configuration
        self.repo_path = self.config['git']['repo_path']
        self.protocol_name = self.config['protocol']['name']
        self.yaml_file_path = self.config['protocol']['yaml_path']
        
        # Processing configuration
        self.max_files = self.config.get('processing', {}).get('max_files', 1)
        self.archive_processed_files = self.config.get('archive_processed_files', False)
        self.validate_after_conversion = self.config.get('processing', {}).get('validate', True)
        
        # Setup progress and error tracking
        self.processed_count = 0
        self.error_count = 0
        
        # Get AWS credentials with flexible fallback strategy
        aws_access_key, aws_secret_key = self._get_aws_credentials()
        
        # Initialize AWS client with retry configuration
        try:
            self.s3 = boto3.client(
                's3',
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                endpoint_url=self.config['aws']['endpoint_url'],
                region_name=self.config['aws']['region'],
                config=boto3.session.Config(
                    s3={'addressing_style': 'path'},
                    retries={'max_attempts': 3, 'mode': 'standard'}
                )
            )
            logger.info(f"AWS S3 client initialized with access key: {aws_access_key[:4]}..." if aws_access_key else "No access key")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            sys.exit(1)
        
        # Initialize Git repository
        try:
            self.repo = Repo(self.repo_path)
            # Check if the repository is valid
            if self.repo.bare:
                logger.error(f"Repository at {self.repo_path} is a bare repository")
                sys.exit(1)
        except git_exc.InvalidGitRepositoryError:
            logger.error(f"Invalid Git repository at {self.repo_path}")
            sys.exit(1)
        except git_exc.NoSuchPathError:
            logger.error(f"No such path: {self.repo_path}")
            sys.exit(1)
        
        # Create temp directory for downloads
        self.temp_dir = tempfile.mkdtemp()
        logger.info(f"Created temporary directory: {self.temp_dir}")
        
        # Working directory for conversions
        self.work_dir = os.path.join(self.temp_dir, 'work')
        os.makedirs(self.work_dir, exist_ok=True)

    def __del__(self):
        """Clean up temporary directory when object is destroyed"""
        try:
            shutil.rmtree(self.temp_dir)
            logger.info(f"Removed temporary directory: {self.temp_dir}")
        except Exception as e:
            logger.error(f"Error removing temporary directory: {e}")

    def _get_aws_credentials(self):
        """Flexibly get AWS credentials from different sources in order of priority"""
        # Define all potential sources for credentials
        sources = [
            # 1. Environment variables (from GitHub Actions or shell)
            {
                'access_key': os.environ.get('AWS_ACCESS_KEY_ID'),
                'secret_key': os.environ.get('AWS_SECRET_ACCESS_KEY'),
                'source_name': 'environment variables'
            },
            # 2. Config file values
            {
                'access_key': self.config['aws'].get('access_key'),
                'secret_key': self.config['aws'].get('secret_key'),
                'source_name': 'config file'
            }
        ]
        
        # Try each source in order
        for source in sources:
            access_key = source['access_key']
            secret_key = source['secret_key']
            
            # If both keys are available from this source, use them
            if access_key and secret_key:
                logger.info(f"Using AWS credentials from {source['source_name']}")
                return access_key, secret_key
        
        # If we got here, we couldn't find valid credentials
        logger.warning("No valid AWS credentials found from any source")
        return None, None

    def list_s3_files(self):
        """List CSV files in S3 bucket with specified prefix and parse versions"""
        try:
            response = self.s3.list_objects_v2(
                Bucket=self.s3_bucket,
                Prefix=self.s3_prefix
            )
            
            if 'Contents' not in response:
                logger.info(f"No files found in s3://{self.s3_bucket}/{self.s3_prefix}")
                return []
            
            # Process and parse files with version information
            parsed_files = []
            for item in response['Contents']:
                if not item['Key'].endswith('.csv'):
                    continue
                    
                filename = os.path.basename(item['Key'])
                
                # Parse DataDict_2019-04-29_0930_revid2218_rev11.csv format
                match = re.match(r'DataDict_(\d{4}-\d{2}-\d{2})_(\d{4})_revid(\d+)_rev(\d+)\.csv', filename)
                if match:
                    date_str, time_str, revid, rev = match.groups()
                    parsed_files.append({
                        'key': item['Key'],
                        'filename': filename,
                        'date': date_str,
                        'time': time_str,
                        'revid': int(revid),
                        'rev': int(rev),
                        'datetime': f"{date_str}_{time_str}",
                        'full_version': f"{date_str}-{time_str}-{revid}-{rev}"
                    })
            
            # Sort by revid (version), then date and time
            sorted_files = sorted(parsed_files, key=lambda x: (x['revid'], x['date'], x['time'], x['rev']))
            
            logger.info(f"Found {len(sorted_files)} CSV files in S3")
            return sorted_files
            
        except Exception as e:
            logger.error(f"Error listing S3 files: {e}")
            return []

    def download_file(self, s3_key, local_path):
        """Download file from S3 to local path with retry logic"""
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                self.s3.download_file(
                    Bucket=self.s3_bucket,
                    Key=s3_key,
                    Filename=local_path
                )
                logger.info(f"Downloaded {s3_key} to {local_path}")
                return True
            except Exception as e:
                retry_count += 1
                logger.warning(f"Error downloading {s3_key} (attempt {retry_count}/{max_retries}): {e}")
                if retry_count >= max_retries:
                    logger.error(f"Failed to download {s3_key} after {max_retries} attempts")
                    return False
        
        return False

    def get_latest_tag_version(self):
        """Retrieve the latest tag version from the repository based on tag names"""
        try:
            tags = sorted(self.repo.tags, key=lambda t: t.name)
            
            if not tags:
                logger.info("No tags found in repository")
                return 0
            
            # The latest tag is the last one in the sorted list
            latest_tag = tags[-1]
            logger.info(f"Latest Tag: {latest_tag.name}")
            
            # Get the commit associated with the latest tag
            commit = latest_tag.commit
            
            # Retrieve the schema file content from the commit
            schema_file_path = f"{self.protocol_name}/{self.protocol_name}_schema"
            try:
                schema_content = (commit.tree / schema_file_path).data_stream.read().decode('utf-8')
                schema_data = json.loads(schema_content)
                version_str = schema_data.get('version', 'revid0')
                version = int(version_str.replace('revid', ''))
                logger.info(f"Latest version from schema: {version}")
                return version
            except KeyError:
                logger.warning(f"Schema file {schema_file_path} not found in tag {latest_tag.name}")
                # Try to extract version from tag name if schema file is missing
                match = re.search(r'revid(\d+)', latest_tag.name)
                if match:
                    version = int(match.group(1))
                    logger.info(f"Extracted version {version} from tag name")
                    return version
                return 0
            except Exception as e:
                logger.error(f"Error reading schema file from tag {latest_tag.name}: {e}")
                return 0
                
        except Exception as e:
            logger.error(f"Error getting latest tag version: {e}")
            return 0

    def update_repo(self, output_folder, folders_to_update):
        """Move specific folders from output folder to the repository"""
        for folder in folders_to_update:
            dest_path = os.path.join(self.repo_path, folder)
            src_path = os.path.join(output_folder, folder)
            
            if not os.path.exists(src_path):
                logger.warning(f"Source path does not exist: {src_path}")
                continue
                
            if os.path.exists(dest_path):
                shutil.rmtree(dest_path)
                logger.info(f"Removed existing folder {dest_path}")
            
            try:
                shutil.copytree(src_path, dest_path)
                logger.info(f"Copied {src_path} to {dest_path}")
            except Exception as e:
                logger.error(f"Copy failed for {src_path}: {e}")
                raise

    def is_version_only_change(self, file_path):
        """Check if the only change in the file is the 'version' field"""
        try:
            with open(file_path, 'r') as f:
                working_content = json.load(f)
            
            # Get content from last commit
            try:
                last_commit_content = self.repo.git.show(f'HEAD:{file_path}')
                last_commit_content = json.loads(last_commit_content)
            except Exception:
                logger.info(f"File {file_path} is new or not in the last commit")
                return False
            
            # Extract and compare versions
            working_version = working_content.pop("version", None)
            last_commit_version = last_commit_content.pop("version", None)
            
            # Compare the rest of the content (excluding version)
            return working_content == last_commit_content and working_version != last_commit_version
        except Exception as e:
            logger.error(f"Error checking version changes for {file_path}: {e}")
            return False

    def process_changed_files(self):
        """Process changed files and discard changes if only 'version' is modified"""
        self.repo.git.add(update=True)  # Ensure index is up to date
        
        changed_files = []
        for item in self.repo.index.diff(None):  # Compare working directory against HEAD
            if item.change_type == 'M' and item.a_path.endswith('.json'):
                file_path = os.path.join(self.repo_path, item.a_path)
                
                if self.is_version_only_change(file_path):
                    self.repo.git.checkout(file_path)
                    logger.info(f"Checked out {file_path} (version-only change)")
                else:
                    logger.info(f"{file_path} has substantial changes")
                    changed_files.append(item.a_path)
        
        return changed_files

    def commit_and_tag(self, commit_message, tag_name, tag_message, folders_to_update):
        """Commit changes and create a tag with a message, including only specified folders"""
        try:
            # Stage only specific folders
            for folder in folders_to_update:
                folder_path = os.path.join(self.repo_path, folder)
                if os.path.exists(folder_path):
                    self.repo.git.add(folder_path)
                    logger.info(f"Added folder to git: {folder_path}")
                else:
                    logger.warning(f"Folder does not exist: {folder_path}")
            
            # Check if there are changes to commit
            if not self.repo.index.diff('HEAD'):
                logger.info("No changes to commit")
                return False
            
            # Commit changes
            self.repo.index.commit(commit_message)
            logger.info(f"Committed with message: {commit_message}")
            
            # Create and push tag
            self.repo.create_tag(tag_name, message=tag_message)
            logger.info(f"Created tag: {tag_name}")
            
            # Push changes to remote
            origin = self.repo.remote(name='origin')
            try:
                origin.push(tag_name)
                origin.push()
                logger.info(f"Pushed tag and changes to remote")
            except git_exc.GitCommandError as e:
                logger.warning(f"Error pushing to remote: {e}")
                logger.info("Attempting force push")
                origin.push(force=True)
                origin.push(tag_name, force=True)
                
            logger.info(f"Git tagged {tag_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error in commit_and_tag: {e}")
            return False

    def process_file(self, file_info):
        """Process a single file for conversion"""
        try:
            s3_key = file_info['key']
            filename = file_info['filename']
            local_path = os.path.join(self.temp_dir, filename)
            
            # Download the file
            if not self.download_file(s3_key, local_path):
                return False
                
            # Get current version from repo
            current_version = self.get_latest_tag_version()
            redcap_version = file_info['revid']
            
            logger.info(f"Processing file {filename}, redcap_version={redcap_version}, current_version={current_version}")
            
            # Check if this is a newer version that needs processing
            if current_version > 0 and redcap_version <= current_version:
                logger.info(f"Skipping {filename}, already processed (version {redcap_version} ≤ {current_version})")
                return False
                
            if redcap_version < 101:  # Assuming minimum version requirement
                logger.info(f"Skipping {filename}, version too low (< 101)")
                return False
                
            # Update YAML with new redcap_version
            with open(self.yaml_file_path, 'r') as f:
                yaml_content = yaml.safe_load(f)
                
            yaml_content['redcap_version'] = f"revid{redcap_version}"
            
            with open(self.yaml_file_path, 'w') as f:
                yaml.safe_dump(yaml_content, f)
                
            logger.info(f"Updated YAML with redcap_version: {yaml_content['redcap_version']}")
            
            # Run the redcap2reproschema command
            cmd = [
                "reproschema",
                "redcap2reproschema",
                local_path,
                self.yaml_file_path,
                "--output-path",
                self.work_dir
            ]
            
            logger.info(f"Running command: {' '.join(cmd)}")
            
            try:
                result = run(cmd, check=True, capture_output=True, text=True)
                logger.info(f"Conversion output: {result.stdout}")
            except CalledProcessError as e:
                logger.error(f"Conversion failed: {e.stderr}")
                return False
            
            # Validate the generated schema if enabled
            output_folder = os.path.join(self.work_dir, self.protocol_name)
            
            if self.validate_after_conversion:
                validate_cmd = ["reproschema", "validate", output_folder]
                logger.info(f"Running validation: {' '.join(validate_cmd)}")
                try:
                    validate_result = run(validate_cmd, check=True, capture_output=True, text=True)
                    logger.info(f"Validation output: {validate_result.stdout}")
                except CalledProcessError as e:
                    logger.error(f"Validation failed: {e.stderr}")
                    return False
            
            # Update repository with new/modified files
            folders_to_update = [f"{self.protocol_name}", "activities"]
            self.update_repo(output_folder, folders_to_update)
            
            # Process changed files (handle version-only changes)
            changed_files = self.process_changed_files()
            
            # If no substantial changes were made, log and return
            if not changed_files and current_version > 0:
                logger.info(f"No substantial changes found in {filename}, skipping commit")
                return False
            
            # Commit and tag the changes
            date_time_str = file_info['datetime']
            commit_message = f"converted {self.protocol_name} redcap data dictionary {date_time_str} to reproschema"
            tag_message = f"redcap data dictionary {date_time_str} to reproschema"
            tag_name = date_time_str.replace("-", ".").replace("_", ".")
            
            if self.commit_and_tag(commit_message, tag_name, tag_message, folders_to_update):
                # Archive processed file in S3 if enabled
                if self.archive_processed_files:
                    archive_key = f"{self.s3_prefix}processed/{filename}"
                    try:
                        self.s3.copy_object(
                            Bucket=self.s3_bucket,
                            CopySource={'Bucket': self.s3_bucket, 'Key': s3_key},
                            Key=archive_key
                        )
                        self.s3.delete_object(Bucket=self.s3_bucket, Key=s3_key)
                        logger.info(f"Archived {s3_key} to {archive_key}")
                    except Exception as e:
                        logger.error(f"Failed to archive file: {e}")
                
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Error processing {filename if 'filename' in locals() else 'file'}: {e}")
            return False

    def run(self):
        """Main method to check for new files and process them"""
        logger.info("Starting ReproSchema conversion CI job")
        
        # Check if reproschema command is available
        try:
            run(["reproschema", "--version"], check=True, stdout=PIPE, stderr=PIPE)
        except (CalledProcessError, FileNotFoundError):
            logger.error("reproschema command not found. Please ensure it's installed.")
            return
        
        start_time = datetime.datetime.now()
        logger.info(f"Job started at {start_time}")
        
        # Pull latest changes from the repository
        try:
            origin = self.repo.remote(name='origin')
            origin.pull()
            logger.info("Pulled latest changes from the repository")
        except git_exc.GitCommandError as e:
            logger.error(f"Error pulling from repository: {e}")
            # Continue anyway, we might have local changes
        
        # List and sort files in S3 bucket
        s3_files = self.list_s3_files()
        if not s3_files:
            logger.info("No files to process")
            return
            
        # Get current version from repo
        current_version = self.get_latest_tag_version()
        logger.info(f"Current repository version: {current_version}")
        
        # Filter for files newer than current version
        if current_version > 0:
            new_files = [f for f in s3_files if f['revid'] > current_version]
            logger.info(f"Found {len(new_files)} files newer than current version {current_version}")
        else:
            new_files = s3_files
            logger.info(f"No current version found, processing all {len(new_files)} files")
        
        # Limit number of files to process
        if self.max_files > 0 and len(new_files) > self.max_files:
            logger.info(f"Limiting to {self.max_files} files per run")
            new_files = new_files[:self.max_files]
        
        # Process each file in sequence
        processed_count = 0
        error_count = 0
        
        for file_info in new_files:
            logger.info(f"Processing file {file_info['filename']} (revid: {file_info['revid']})")
            
            if self.process_file(file_info):
                processed_count += 1
                logger.info(f"Successfully processed {file_info['filename']}")
            else:
                error_count += 1
                logger.warning(f"Failed to process {file_info['filename']}")
            
            # Break after processing first successful file if starting from scratch
            if current_version == 0 and processed_count > 0:
                logger.info("Initial conversion completed. Stopping to establish baseline.")
                break
        
        end_time = datetime.datetime.now()
        duration = end_time - start_time
        
        logger.info(f"Job completed at {end_time}")
        logger.info(f"Duration: {duration}")
        logger.info(f"Processed {processed_count} files with {error_count} errors")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='ReproSchema Conversion CI')
    parser.add_argument('--config', type=str, default='config/config.yaml',
                      help='Path to configuration file (default: config/config.yaml)')
    args = parser.parse_args()
    
    converter = ReproSchemaConverter(config_path=args.config)
    converter.run()