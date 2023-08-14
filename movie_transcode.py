import os
import csv
import argparse
import subprocess
import datetime
import shutil
import sys
import time
import platform
from wcwidth import wcswidth
from prettytable import PrettyTable

MOVIE_EXTENSIONS = {
    '.avi', '.divx', '.m4v', '.mkv', '.mp4', '.mpg', '.wmv', '.vob',
    '.3gp', '.3g2', '.asf', '.flv', '.m2v', '.mov', '.mts', '.m2ts',
    '.ogv', '.rm', '.rmvb', '.ts', '.webm', '.f4v'
}

def compute_quality_index(height, width, bitrate: float) -> int:
    if bitrate == "N/A":
        return 0  # Or any default value you wish to assign when the bitrate is unknown

    # Constants for scoring
    RESOLUTION_TIERS = [0, 480, 720, 1080, 2160]  # Assuming 4K as the next step
    RESOLUTION_SCORES = [0, 25, 50, 75, 100]  # Corresponding scores

    BITRATE_TIERS = {
        480: [0, 1],
        720: [1, 2.5, 5],
        1080: [2.5, 5, 10, 20],
        2160: [5, 10, 20, 40]  # Assumed values for 4K
    }
    BITRATE_SCORES = {
        480: [0, 15],
        720: [15, 25, 40],
        1080: [25, 35, 45, 60],
        2160: [35, 45, 60, 80]  # Assumed scores for 4K
    }

    # Linear interpolation function
    def interpolate(x, x_values, y_values):
        for i, xi in enumerate(x_values):
            if x < xi:
                break
        x1, x2 = x_values[i - 1], xi
        y1, y2 = y_values[i - 1], y_values[i]
        if (x2 - x1) == 0:  # Avoid division by zero
            return y1
        return y1 + (x - x1) * (y2 - y1) / (x2 - x1)

    # Compute resolution score
    resolution_score = interpolate(height, RESOLUTION_TIERS, RESOLUTION_SCORES)

    # Get the resolution tier for bitrate calculation
    for i, r in enumerate(RESOLUTION_TIERS[1:], 1):  # skip the 0 tier
        if height < r:
            res_tier = RESOLUTION_TIERS[i - 1]
            break
    else:
        res_tier = RESOLUTION_TIERS[-1]  # For height >= highest defined resolution tier

    # This is to ensure that we always get a valid res_tier for the given height
    if height < RESOLUTION_TIERS[1]:
        res_tier = RESOLUTION_TIERS[1]

    # Compute bitrate score
    bitrate_score = interpolate(bitrate, BITRATE_TIERS[res_tier], BITRATE_SCORES[res_tier])

    # Total score is an average of resolution and bitrate scores
    score = (resolution_score + bitrate_score) / 2

    return int(score)

def get_movie_details(filepath):
    # Commands for ffprobe
    cmd_duration = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
    cmd_resolution = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", filepath]
    cmd_bitrate = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=bit_rate", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
    
    # Extract duration
    duration_output = subprocess.check_output(cmd_duration, stderr=subprocess.STDOUT, text=True).strip()
    try:
        duration_secs = float(duration_output)
        duration = str(datetime.timedelta(seconds=int(duration_secs)))
    except Exception as e:
        duration = "N/A"

    # Extract resolution
    resolution_output = subprocess.check_output(cmd_resolution, stderr=subprocess.STDOUT, text=True).strip()
    values = [int(val) for val in resolution_output.split(',') if val and val.isdigit()]
    if len(values) == 2:
        width, height = values
    else:
        width, height = "N/A", "N/A"

    # Extract bitrate (and convert from bps to Mbps)
    try:
        bitrate_output = subprocess.check_output(cmd_bitrate, stderr=subprocess.STDOUT, text=True).strip()
        
        # Check if the output is numeric
        if bitrate_output.replace('.', '', 1).isdigit():
            bitrate = float(bitrate_output) / (10**6)
        else:
            bitrate = "N/A"
            
    except Exception as e:
        print(f"Error processing bitrate of file: {filepath}. Error: {e}")
        bitrate = "N/A"

    return duration, width, height, bitrate

def adjusted_width(string, desired_length):
    # Calculate the discrepancy in length
    discrepancy = len(string) - wcswidth(string)
    adjusted_length = desired_length + discrepancy
    return adjusted_length

def get_display_string(s, length):
    # If the display width of s is greater than length
    if wcswidth(s) > length:
        while wcswidth(s) > length - 3:  # 3 for "..."
            s = s[:-1]
        s += "..."
    return s

def get_free_space(file_path: str) -> str:
    # Get the free space for the filesystem containing the given file path
    if platform.system() == "Windows":
        drive = os.path.splitdrive(file_path)[0] + os.path.sep
        free_bytes = os.stat(drive).st_free
    else:
        space_stats = os.statvfs(file_path)
        free_bytes = space_stats.f_bavail * space_stats.f_frsize

    # Determine the unit for the space and format the value
    if free_bytes < 10**9:  # less than 1 GB
        space_val = free_bytes / (10**6)  # Convert to MB
        unit = "MB"
    elif free_bytes < 10**12:  # less than 1 TB
        space_val = free_bytes / (10**9)  # Convert to GB
        unit = "GB"
    else:
        space_val = free_bytes / (10**12)  # Convert to TB
        unit = "TB"

    return f"{space_val:.3f} {unit}"

def get_gpu_info():
    if platform.system() == "Windows":
        try:
            result = subprocess.check_output(["wmic", "path", "win32_VideoController", "get", "name"])
            return result.decode('utf-8').strip().split("\n")[1]
        except:
            return None
    elif platform.system() == "Darwin":  # macOS
        try:
            result = subprocess.check_output(["system_profiler", "SPDisplaysDataType"])
            return result.decode('utf-8')
        except:
            return None
    else:
        return None  # Linux (you can add a method to detect GPU on Linux if needed)

def get_hw_acceleration():
    system = platform.system()
    processor = platform.processor()

    if system == "Windows":
        gpu_info = get_gpu_info()
        if "NVIDIA" in gpu_info:
            return "h264_nvenc"
        else:
            return "h264_qsv"  # Assumes Intel QuickSync for non-NVIDIA cards. Adjust as necessary.
    
    elif system == "Darwin":  # macOS
        gpu_info = get_gpu_info()
        if "Intel" in gpu_info:
            return "h264_videotoolbox"  # For Intel integrated GPUs on macOS
        elif "AMD" in gpu_info or "Radeon" in gpu_info:
            return "h264_amf"  # For AMD GPUs on macOS
        else:
            return "h264_videotoolbox"  # Default to VideoToolbox for macOS
    
    elif system == "Linux":
        # This is a simple approach for Linux, and may not cover all scenarios
        if "intel" in processor.lower():
            return "h264_vaapi"  # For Intel processors on Linux
        else:
            return "libx264"  # Default software encoding on Linux

    else:
        return "libx264"  # Fallback to software encoding

def transcode(file_path, original_bitrate, resolution, working_directory, dry_run=False, local_operations=False):
    # If local_operations flag is set.
    if local_operations:
        # Extract filename from the path.
        filename = os.path.basename(file_path)
        
        # Define local path.
        local_path = os.path.join(os.path.expanduser("~"), filename)
        
        # Copy file to local path.
        shutil.copy(file_path, local_path)
        
        # Transcode using local path.
        new_file_size, new_file_name, transcode_status, target_file_name = transcode_internal(local_path, original_bitrate, resolution, working_directory, dry_run=dry_run)
        
        if transcode_status == "done":
            # Extract just the filename without any directory information.
            filename_only = os.path.basename(new_file_name)

            new_file_name = os.path.join(os.path.expanduser("~"), filename_only)
            
            # Move the transcoded file back to remote location.
            output_remote_path = os.path.join(os.path.dirname(file_path), filename_only)

            # Check if the remote file already exists
            if os.path.exists(output_remote_path):
                base_name, ext = os.path.splitext(filename_only)
                
                # Increment counter until we find a filename that doesn't exist
                counter = 1
                while os.path.exists(output_remote_path):
                    # Append a counter to the file name to make it unique
                    new_file_name_with_counter = f"{base_name} ({counter}){ext}"
                    output_remote_path = os.path.join(os.path.dirname(file_path), new_file_name_with_counter)
                    counter += 1

            try:
                shutil.move(new_file_name, output_remote_path)  # Updated the source path here
            except FileNotFoundError:
                print(f"Error: The source file {new_file_name} was not found.")
            except PermissionError:
                print(f"Error: Permission denied. Can't move {new_file_name} to {output_remote_path}.")
            except Exception as e:
                print(f"An error occurred while moving the file: {e}")

            # Cleanup local files.
            if os.path.exists(local_path):  # Extra check just in case.
                os.remove(local_path)

            if os.path.exists(new_file_name):  # Extra check just in case.
                os.remove(new_file_name)
            
            # Return using the remote path.
            return new_file_size, output_remote_path, transcode_status, target_file_name
        else:
            # If there was an issue with transcoding, return the local status and paths.
            return new_file_size, new_file_name, transcode_status, target_file_name
    else:
        return transcode_internal(file_path, original_bitrate, resolution, working_directory, dry_run=dry_run)

def transcode_internal(file_path, original_bitrate, resolution, working_directory, dry_run=False):
    start_time = time.time()  # Start the timer

    # Extract the file name without the extension and its directory path
    file_name_no_ext = os.path.splitext(os.path.basename(file_path))[0]
    directory = os.path.dirname(file_path)
    
    # Check free space
    stat = os.statvfs(directory)
    free_space = stat.f_frsize * stat.f_bavail  # Total free space in bytes
    original_file_size = os.path.getsize(file_path)
    
    # Ensure at least the original file size + 2GB is available
    if free_space < original_file_size + (2 * 1024**3):
        end_time = time.time()  # End the timer
        elapsed_time = end_time - start_time
        print(f"Time taken: {elapsed_time:.2f} seconds")
        print("Insufficient space on the drive. Ensure at least the original file size + 2GB is available.")
        return 0, "", "failed", ""
    
    # Generate new file name with .mp4 extension
    new_file_name = f"{file_name_no_ext}.mp4"
    new_file_path = os.path.join(directory, new_file_name)

    # If dry run, skip the actual transcoding
    if dry_run:
        end_time = time.time()  # End the timer
        elapsed_time = end_time - start_time
        #print(f"Time taken (dry run): {elapsed_time:.2f} seconds")
        return 0, new_file_name, "skipped", ""
    
    # Check if the new file path already exists and its extension is .mp4
    target_file_path = new_file_path
    if os.path.exists(new_file_path) and new_file_path.lower().endswith('.mp4'):
        base_name = f"{file_name_no_ext}"
        counter = 1
        while os.path.exists(new_file_path):
            # Append a counter to the file name to make it unique
            new_file_name = f"{base_name} ({counter}).mp4"
            new_file_path = os.path.join(directory, new_file_name)
            counter += 1

    encoder = get_hw_acceleration()

    cmd = [
        "ffmpeg",
        "-i", file_path,
        "-threads", "0",
        "-c:v", encoder,
        "-acodec", "aac",
        "-q:v", "70",
        "-movflags", "+faststart",      # Adding support for faststart
        #"-preset", "veryfast",
        new_file_path
    ]

    # Execute the command and capture any errors
    sp_result = subprocess.run(cmd, capture_output=True)
    result = sp_result.stdout.decode('utf-8', errors='replace')

    # Check for errors and determine status
    if sp_result.returncode != 0:
        end_time = time.time()  # End the timer
        elapsed_time = end_time - start_time
        print(f"Time taken (failed): {elapsed_time:.2f} seconds")
        print(sp_result.stderr.decode('utf-8', errors='replace'))
        return 0, new_file_name, "failed", ""

    # Calculate new file size
    new_file_size = os.path.getsize(new_file_path)

    end_time = time.time()  # End the timer
    elapsed_time = end_time - start_time
    #print(f"Time taken: {elapsed_time:.2f} seconds")
    
    return new_file_size, new_file_name, "done", target_file_path

def move_to_safe_folder(original_file_path):
    # Get the directory containing the original file
    directory = os.path.dirname(original_file_path)
    
    # Define the path for the safe folder
    safe_folder = os.path.join(directory, "_137particles_safe")
    
    # Check if the safe folder exists, if not, create it
    if not os.path.exists(safe_folder):
        os.mkdir(safe_folder)
    
    # Move the original file to the safe folder
    shutil.move(original_file_path, os.path.join(safe_folder, os.path.basename(original_file_path)))

def print_arguments_table(args):
    description_mapping = {
        "source_directory": "Source Directory for Movies",
        "dry_run": "Dry Run (No Actual Renaming)",
        "fast_scan": "Fast Scan (Skip Already Renamed)",
        "working_directory": "Working Directory for Transcoding"  # New description for the working_directory argument
    }

    table = PrettyTable()
    table.field_names = ["Argument Description", "Value"]
    table.align["Argument Description"] = "l"
    table.align["Value"] = "l"

    for arg_name, arg_value in vars(args).items():
        description = description_mapping.get(arg_name, arg_name)
        table.add_row([description, arg_value])

    print(table)

def get_media_format(filepath):
    # Get video format
    cmd_video = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
    try:
        video_format = subprocess.check_output(cmd_video, stderr=subprocess.STDOUT, text=True).strip()
        if 'Header missing' in video_format or not video_format:
            video_format = "N/A"
    except subprocess.CalledProcessError as e:
        print(f"Error processing video stream of file: {filepath}. Error: {e.output}")
        video_format = "N/A"

    # Get audio format
    cmd_audio = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
    try:
        audio_format = subprocess.check_output(cmd_audio, stderr=subprocess.STDOUT, text=True).strip()
        if 'Header missing' in audio_format or not audio_format:
            audio_format = "N/A"
    except subprocess.CalledProcessError as e:
        print(f"Error processing audio stream of file: {filepath}. Error: {e.output}")
        audio_format = "N/A"

    return video_format, audio_format

def main(directory_path, dry_run, fast_scan, working_directory):
    processed_files = set()

    # Check if media_info_log.csv exists when using --fast_scan
    if fast_scan and os.path.exists('media_info_log.csv'):
        with open('media_info_log.csv', 'r', newline='') as log_file:
            reader = csv.reader(log_file)
            # skip the header
            next(reader)
            # add processed filenames to the set
            for row in reader:
                processed_files.add(row[0])

    filename = '_137particles_movie_transcode_log.csv'
    file_exists = os.path.exists(filename)

    with open(filename, 'a', newline='') as log_file:
        # Convert the directory_path to an absolute path
        absolute_directory_path = os.path.abspath(directory_path)
        total_free_space = get_free_space(absolute_directory_path)

        print(f"{absolute_directory_path}")

        print(f"{total_free_space} available on target device")
        print(f"Using {get_hw_acceleration()} HW encoder")

        log_writer = csv.writer(log_file)
        if not file_exists:  
            log_writer.writerow(["File Name", "Extension", "Video Format", "Audio Format", "Duration", "Width", "Height", "Bitrate", "Quality", "Is Target Format?", "Timestamp", "Original File Size", "New File Size", "Percent Change", "New File Name", "Transcode Status"])

        # Define column widths
        col_widths = {
            'index': 5,
            'file_name': 100,
            'extension': 10,
            'video_format': 15,
            'audio_format': 15,
            'duration': 12,
            'resolution': 15,
            'bitrate': 12,
            'quality': 12,
            'orig_size': 20,  # Increase for padding
            'new_size': 20,  # Increase for padding
            'change': 12,
            'status': 10
        }

        # Print header
        header = (f"{'#':<{col_widths['index']}} "
                  f"{'File Name':<{col_widths['file_name']}} "
                  f"{'Extension':<{col_widths['extension']}} "
                  f"{'Video Format':<{col_widths['video_format']}} "
                  f"{'Audio Format':<{col_widths['audio_format']}} "
                  f"{'Duration':<{col_widths['duration']}} "
                  f"{'Resolution':<{col_widths['resolution']}} "
                  f"{'Bitrate':<{col_widths['bitrate']}} "
                  f"{'Quality':<{col_widths['quality']}} "
                  f"{'Orig. Size (MB)':>{col_widths['orig_size']}} "  # -3 accounts for the " MB"
                  f"{'New Size (MB)':>{col_widths['new_size']}} "     # -3 accounts for the " MB"
                  f"{'% Change':>{col_widths['change'] - 1}} "             # -1 accounts for the "%"
                  f"{'Status':<{col_widths['status']}}")
        separator = '-' * len(header)

        print(header)
        print(separator)

        processed_count = 0

        file_list = os.listdir(directory_path)
        file_list.sort()

        for filename in file_list:
            if filename in processed_files:
                continue

            file_ext = os.path.splitext(filename)[1]
            if file_ext.lower() not in MOVIE_EXTENSIONS:
                continue

            # Print the placeholder
            sys.stdout.write(f"\nProcessing file {filename}...")
            sys.stdout.flush()  # Ensure the output is displayed immediately

            video_format, audio_format = get_media_format(os.path.join(directory_path, filename))
            timestamp = datetime.datetime.now().isoformat()

            original_file_size = os.path.getsize(os.path.join(directory_path, filename))   
            new_file_size = 0
            percent_change = 0         

            # check if video and audio formats match the desired qualities
            is_target_format = (file_ext.lower() == '.mp4' and video_format == 'h264' and audio_format == 'aac')

            duration, width, height, bitrate = get_movie_details(os.path.join(directory_path, filename))

            if height != "N/A" and int(height) < 720:
                resolution_display = f" {width}x{height}"
            elif height != "N/A":
                resolution_display = f" {width}x{height}"            
            else:
                resolution_display = "N/A"

            if height != "N/A" and bitrate != "N/A" and bitrate != 0:
                quality = compute_quality_index(height, width, float(bitrate))
            else:
                quality = "0"

            # Display on console
            filename_display = get_display_string(filename, 100)

            if not is_target_format:
                new_file_size, new_file_name, transcode_status, target_file_name = transcode(os.path.join(directory_path, filename), bitrate, (width, height), working_directory, dry_run, True)
            else:
                transcode_status = "ignored"
                new_file_size = original_file_size  # Use the original size if not transcoding
                new_file_name = filename
                target_file_name = filename
                
            original_file_size_mb = original_file_size / (1024 * 1024)
            new_file_size_mb = new_file_size / (1024 * 1024)

            # Ensure you calculate the percentage change before logging it
            percent_change = ((new_file_size - original_file_size) / original_file_size) * 100 if original_file_size else 0

            # Write to CSV
            log_writer.writerow([filename, file_ext, video_format, audio_format, duration, width, height, bitrate, quality, is_target_format, timestamp, original_file_size_mb, new_file_size_mb, percent_change, new_file_name, transcode_status])
            log_file.flush()

            if transcode_status == "done" and not dry_run:
                move_to_safe_folder(os.path.join(directory_path, filename))

                if target_file_name != "":
                    if os.path.basename(new_file_name) != os.path.basename(target_file_name):
                        # Create full paths for clarity and accurate checking
                        new_file_path = os.path.join(directory_path, os.path.basename(new_file_name))
                        target_file_path = os.path.join(directory_path, os.path.basename(target_file_name))

                        # Check if the source file exists and target file doesn't exist
                        if os.path.exists(new_file_path):
                            if not os.path.exists(target_file_path):
                                os.rename(new_file_path, target_file_path)
                                #print(f"Renamed {new_file_name} to {target_file_name} successfully.")
                            else:
                                print(f"{target_file_name} already exists in {directory_path}. Rename aborted to avoid overwriting.")
                        else:
                            print(f"{new_file_name} does not exist in {directory_path}.")

            terminal_width = shutil.get_terminal_size().columns
            sys.stdout.write('\r' + ' ' * terminal_width + '\r')
            sys.stdout.flush()

            processed_count += 1
            sys.stdout.write(f"{processed_count:<{col_widths['index']}} "
                f"{filename_display:<{col_widths['file_name']}} "
                f"{file_ext:<{col_widths['extension']}} "
                f"{video_format:<{col_widths['video_format']}} "
                f"{audio_format:<{col_widths['audio_format']}} "
                f"{duration:<{col_widths['duration'] - 1}} "
                f"{resolution_display:<{col_widths['resolution']}} "
                f"{bitrate:<{col_widths['bitrate']}} "
                f"{quality:<{col_widths['quality']}} "
                f"{original_file_size_mb:>{col_widths['orig_size'] - 3}.2f} MB "
                f"{new_file_size_mb:>{col_widths['new_size'] - 3}.2f} MB "
                f"{percent_change:>{col_widths['change'] - 1}.2f}% "
                f"{transcode_status:<{col_widths['status']}}")
            sys.stdout.flush()

            #if not is_target_format:
            #    break
            
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Evaluate media files and log their internal formats.")
    parser.add_argument('source_directory', help="Path to the directory containing movie files.")
    parser.add_argument('--dry_run', action='store_true', help="Show renaming actions without executing them.")
    parser.add_argument('--fast_scan', action='store_true', help="Skip files previously renamed as per the log.")
    parser.add_argument('--working_directory', default=os.path.expanduser("~"), help="Path to the directory to be used for temporary working files during transcoding. Defaults to user's home directory.")

    args = parser.parse_args()
    
    print_arguments_table(args)
    
    main(args.source_directory, args.dry_run, args.fast_scan, args.working_directory)



#file 'Unabomber.In.His.Own.Words.1of4.1080p.WEB.x264.AC3.MVGroup.org.mkv'
#file 'Unabomber.In.His.Own.Words.2of4.1080p.WEB.x264.AC3.MVGroup.org.mkv'
#file 'Unabomber.In.His.Own.Words.3of4.1080p.WEB.x264.AC3.MVGroup.org.mkv'
#file 'Unabomber.In.His.Own.Words.4of4.1080p.WEB.x264.AC3.MVGroup.org.mkv'

#ffmpeg -f concat -safe 0 -i files.txt \
#  -c:v h264_videotoolbox \
#  -q:v 60 \
#  Unabomber.mp4