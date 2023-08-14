import os
import re
import argparse
from datetime import datetime
import csv
import difflib
import sys
import openai
import shutil
from imdb import IMDb
from prettytable import PrettyTable
from wcwidth import wcswidth
from dotenv import load_dotenv


# Load .env file
load_dotenv()

# Initialization
openai_key = os.getenv('OPENAI_KEY')
if not openai_key:
    raise ValueError("OPENAI_KEY not set in .env file")
openai.api_key = openai_key

MOVIE_EXTENSIONS = {
    '.avi', '.divx', '.m4v', '.mkv', '.mp4', '.mpg', '.wmv', '.vob',
    '.3gp', '.3g2', '.asf', '.flv', '.m2v', '.mov', '.mts', '.m2ts',
    '.ogv', '.rm', '.rmvb', '.ts', '.webm', '.f4v'
}

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

def print_arguments_table(args):
    description_mapping = {
        "source_directory": "Source Directory for Movies",
        "dry_run": "Dry Run (No Actual Renaming)",
        "fast_scan": "Fast Scan (Skip Already Renamed)",
        "skip_if_confidence_below": "Minimum Confidence Threshold for Renaming"
    }

    table = PrettyTable()
    table.field_names = ["Argument Description", "Value"]
    table.align["Argument Description"] = "l"
    table.align["Value"] = "l"

    for arg_name, arg_value in vars(args).items():
        description = description_mapping.get(arg_name, arg_name)
        table.add_row([description, arg_value])

    print(table)

# Define a function to sanitize a string for filenames
def sanitize_filename(filename):
    forbidden_chars = r'<>"/\|?*'  # List of forbidden characters in Windows
    sanitized_filename = ''.join(char if char not in forbidden_chars else '_' for char in filename)
    return sanitized_filename

def compute_confidence(original, renamed):
    # 1. Compute normalized Levenshtein distance
    lev_dist = difflib.SequenceMatcher(None, original, renamed).ratio()
    
    # 2. Token matching
    original_tokens = set(original.split())
    renamed_tokens = set(renamed.split())
    common_tokens = original_tokens.intersection(renamed_tokens)
    token_ratio = len(common_tokens) / max(len(original_tokens), len(renamed_tokens))
    
    # 3. Length ratio
    len_ratio = min(len(original), len(renamed)) / max(len(original), len(renamed))
    
    # Combine metrics for final confidence (weights can be adjusted)
    confidence = 0.5 * lev_dist + 0.3 * token_ratio + 0.2 * len_ratio

    return confidence

def refine_title(title):
    annotations = [
        "Anniversary Edition", "Director's Cut", "Extended Edition", 
        "Special Edition", "Remastered", "Unrated", "Re-release",
        "Unreleased Director's Cut"
    ]
    pattern = r'\b(?:' + '|'.join(re.escape(anno) for anno in annotations) + r')\b'
    new_title = re.sub(pattern, '', title).strip()
    new_title = new_title.replace("()", "").strip()
    return re.sub(' +', ' ', new_title)

def extract_title_year(filename):
    current_year = datetime.now().year
    
    # 1. Remove file extension
    no_ext = os.path.splitext(filename)[0]

    # 2. Remove text enclosed within square brackets
    no_brackets = re.sub(r'\[.*?\]', '', no_ext)

    # First, attempt to find a year within parentheses
    match = re.search(r'(.+?)\s*\((\d{4})\)', no_brackets)
    if match:
        title = match.group(1).replace('.', ' ').strip()
        year = match.group(2)
        
        # Check if the year is within a valid range
        if 1888 <= int(year) <= current_year:
            return title, year
        else:
            return title, None

    # If the year isn't in parentheses, attempt to find it elsewhere
    match = re.search(r'(.+?)\.(\d{4})\.', no_brackets)
    if match:
        title = match.group(1).replace('.', ' ').strip()
        year = match.group(2)
        
        # Check if the year is within a valid range
        if 1888 <= int(year) <= current_year:
            return title, year
        else:
            return title, None

    # If both attempts fail, return title without a year
    title = no_brackets.replace('.', ' ').strip()
    return title, None

def lookup_movie(title, filename, year=None):
    ia = IMDb()
    
    refined_title = refine_title(title)
    movies = ia.search_movie(refined_title)

    # If the refined title search fails, try the full title
    if not movies:
        movies = ia.search_movie(title)
    
   # Filter results to keep only movies (kind='movie')
    movies = [movie for movie in movies if movie.get('kind') == 'movie']

    if movies and year:
        movies = [movie for movie in movies if 'year' in movie and movie['year'] == int(year)]
    
    # Sort movies by similarity to the original title
    movies = sorted(movies, key=lambda m: -difflib.SequenceMatcher(None, m['title'], title).ratio())
    
    if movies:
        # Safely get the year from the movie data, default to 'Unknown' if not available
        selected_year = movies[0].get('year', 'Unknown')
        return movies[0]['title'], selected_year, False  # False indicates IMDb source
    else:
        temperature_value = 0.2

        year_info = f" from the year {year}" if year else ""
        prompt = (f"You are tasked with determining a probable movie name from a filename. "
                f"The original filename is '{filename}', which hints at a movie titled '{title}'{year_info}. "
                f"If you recognize this from your movie knowledge, provide its accurate title and release year in the format 'Movie Name (YEAR)'. "
                f"However, if you are uncertain or the result seems overly long or unrelated, revert to the original filename '{filename}'. "
                f"For movies that might be too recent or unknown, make your best intuitive guess. Always aim for concise and relevant titles.")

        response = openai.Completion.create(
            engine="text-davinci-003", 
            prompt=prompt,
            max_tokens=250,
            temperature=temperature_value
        )
        result = response.choices[0].text.strip()
        match = re.search(r'(.+?)\s*\((\d{4})\)', result)
        if match:
            return match.group(1), match.group(2), True  # True indicates GPT-3.5 source
        else:
            return None, None, True

def main(directory_path, dry_run, fast_scan=False, skip_if_confidence_below=0.22):
    renamed_count = 0
    MAX_FILENAME_LENGTH = 120

    # If fast_scan is enabled and rename_log.csv exists, read previously renamed files from the log
    previously_renamed_files = set()
    file_log_name = "_137particles_movie_naming_log.csv"

    if fast_scan and os.path.exists(file_log_name):
        with open(file_log_name, 'r', newline='') as log_file:
            log_reader = csv.reader(log_file)
            next(log_reader)  # Skip header row
            for row in log_reader:
                _, new_name, _ = row
                previously_renamed_files.add(new_name)

    # Open a CSV file to log changes
    with open(file_log_name, 'a', newline='') as log_file:
        log_writer = csv.writer(log_file)

        if not fast_scan:  # Only write the header if it's not a fast scan
            log_writer.writerow(["Original Name", "New Name", "Timestamp"])

        file_list = os.listdir(directory_path)
        file_list.sort()

        for filename in file_list:
            # Exclude previously renamed files in fast_scan mode
            if filename in previously_renamed_files:
                continue

            # Skip if [DNP] tag is present in the filename
            if "[DNP]" in filename:
                print(f"\nSkipping (Due to [DNP] tag): {filename}")
                continue

            # Check if the file has a movie extension
            file_ext = os.path.splitext(filename)[1]
            if file_ext.lower() not in MOVIE_EXTENSIONS:
                continue

            # Print the placeholder
            sys.stdout.write(f"\nProcessing file {filename}...")
            sys.stdout.flush()  # Ensure the output is displayed immediately

            title, year = extract_title_year(filename)
            if title:
                imdb_title, imdb_year, from_gpt = lookup_movie(title, filename, year)
                if imdb_title and imdb_year:
                    # Increment renamed count
                    renamed_count += 1
                    
                    new_name = f"{imdb_title} ({imdb_year}){file_ext}"
                    new_name = sanitize_filename(new_name)

                    # Compute confidence
                    confidence = compute_confidence(filename, new_name)

                    # Formatting for tabular output
                    source = "GPT-3.5" if from_gpt else "IMDb"
                    display_name = new_name

                    if len(display_name) > 100:
                        display_name = display_name[:97] + "..."

                    filename_width = adjusted_width(filename, 100)
                    new_name_width = adjusted_width(display_name, 100)

                    terminal_width = shutil.get_terminal_size().columns
                    sys.stdout.write('\r' + ' ' * terminal_width + '\r')
                    sys.stdout.flush()
                    
                    sys.stdout.write(f"{renamed_count:5} {filename:<{filename_width}} {display_name:<{new_name_width}} {confidence:<10.3f} {source:<10}")
                    sys.stdout.flush()
                    
                    # Write the change to the CSV file                
                    # Get the current time in ISO 8601 format
                    timestamp = datetime.now().isoformat()

                    log_writer.writerow([filename, new_name, timestamp])
                    log_file.flush()  # Immediately flush the buffer to write to the file
                
                    # Check if filenames are different and if not a dry run, rename the file
                    if filename != new_name and confidence > skip_if_confidence_below and len(new_name) < MAX_FILENAME_LENGTH:
                        if not dry_run:
                            new_path = os.path.join(directory_path, new_name)

                            # Check if the new filename already exists
                            if os.path.exists(new_path):
                                #print(f"\t\tWarning: {new_name} already exists. Skipping renaming of {filename}.")
                                skipped = True
                            else:
                                os.rename(os.path.join(directory_path, filename), new_path)
                else:
                    print(f"Couldn't identify movie for file: {filename}")
            else:
                print(f"Couldn't extract title/year from file: {filename}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Rename movie files to match IMDb titles and years.")
    parser.add_argument('source_directory', help="Path to the directory containing movie files.")
    parser.add_argument('--dry_run', action='store_true', help="Show renaming actions without executing them.")
    parser.add_argument('--fast_scan', action='store_true', help="Skip files previously renamed as per the log.")
    parser.add_argument('--skip_if_confidence_below', type=float, default=0.22, help="Skip renaming if confidence is below this threshold.")
    
    args = parser.parse_args()

    print_arguments_table(args)

    main(args.source_directory, args.dry_run, args.fast_scan, args.skip_if_confidence_below)