import requests
import datetime
import re
from icalendar import Calendar, Event
from todoist_api_python.api import TodoistAPI
import os

# --- Configuration ---
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN")
USER_ICS_URL = os.environ.get("USER_ICS_URL")
SCHEMA_ICS_URL = os.environ.get("SCHEMA_ICS_URL")
ADDED_EVENTS_FILE = "added_events.txt"  # File to track already added events

if not TODOIST_API_TOKEN or not USER_ICS_URL or not SCHEMA_ICS_URL:
    print("Error: Please set the TODOIST_API_TOKEN, USER_ICS_URL, and SCHEMA_ICS_URL environment variables in Render/GitHub Secrets.")
    exit()

def load_calendar(url):
    """Loads an iCalendar from a given URL."""
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for bad status codes
        return Calendar.from_ical(response.text)
    except requests.exceptions.RequestException as e:
        print(f"Error loading calendar from {url}: {e}")
        return None

def clean_text(text):
    """Removes extra whitespace and converts to lowercase for comparison."""
    return re.sub(r'\s+', ' ', text).strip().lower()

def extract_lecture_title(summary):
    """
    Attempts to extract the central part of the lecture title.
    Example:
      "Program: ... Laboratoriemedicin vår T3 [BMA401 VT25]" -> "laboratoriemedicin vår t3"
    If "laboratoriemedicin" is found, returns text from that word
    until "sign:" or "moment:" if present.
    Otherwise, returns the entire summary.
    """
    summary_clean = clean_text(summary)
    idx = summary_clean.find("laboratoriemedicin")
    if idx != -1:
        sub = summary_clean[idx:]
        m = re.search(r'(sign:|moment:)', sub)
        if m:
            return sub[:m.start()].strip()
        else:
            return sub.strip()
    return summary.strip()

def find_schema_times(user_event, schema_events):
    """
    For an event in the user's calendar with only a date (no time),
    searches the schema calendar for an event with the same date
    where the cleaned title (based on extract_lecture_title) matches (substring).
    Returns (dtstart, dtend) from the schema event if a match is found, otherwise None.
    """
    dtstart_field = user_event.get('dtstart')
    if not dtstart_field:
        return None
    user_date = dtstart_field.dt if isinstance(dtstart_field.dt, datetime.date) else dtstart_field.dt.date()
    user_title = extract_lecture_title(user_event.get('summary', ''))

    for se in schema_events:
        schema_dtstart = se.get('dtstart')
        if not isinstance(schema_dtstart.dt, datetime.datetime):
            continue
        schema_date = schema_dtstart.dt.date()
        if schema_date == user_date:
            schema_title = extract_lecture_title(se.get('summary', ''))
            if (user_title in schema_title) or (schema_title in user_title):
                return se.get('dtstart').dt, se.get('dtend').dt
    return None

def adjust_zoom_title(title, event):
    """
    If the event's location or description contains "zoom" (lowercase),
    adds "Zoom " to the beginning of the title (if it's not already there).
    """
    loc = event.get('location', '')
    desc = event.get('description', '')
    if ("zoom" in loc.lower()) or ("zoom meeting" in desc.lower()):
        if not title.lower().startswith("zoom "):
            return "Zoom " + title
    return title

def load_added_events():
    """Loads the list of already added event identifiers from the tracking file."""
    added_events = set()
    if os.path.exists(ADDED_EVENTS_FILE):
        with open(ADDED_EVENTS_FILE, 'r') as f:
            for line in f:
                added_events.add(line.strip())
    return added_events

def save_added_events(added_events):
    """Saves the set of added event identifiers to the tracking file."""
    with open(ADDED_EVENTS_FILE, 'w') as f:
        for event_id in added_events:
            f.write(f"{event_id}\n")

def generate_event_id(event):
    """Generates a unique identifier for an event based on its summary and start time."""
    summary = event.get('summary', '')
    dtstart_field = event.get('dtstart')
    dtstart_str = str(dtstart_field.dt) if dtstart_field else ''
    return f"{summary}-{dtstart_str}"

def sync_calendar_to_todoist():
    """
    Fetches calendar events, filters them, and adds them as tasks to Todoist,
    skipping events that have already been added.
    """
    api = TodoistAPI(TODOIST_API_TOKEN)
    try:
        api.sync() # This might not be necessary with the new library, but keep it for now
    except Exception as e:
        print(f"Error syncing with Todoist API: {e}")
        return

    user_cal = load_calendar(USER_ICS_URL)
    schema_cal = load_calendar(SCHEMA_ICS_URL)

    if not user_cal or not schema_cal:
        print("Failed to load one or both calendars. Exiting.")
        return

    schema_events = [comp for comp in schema_cal.walk() if comp.name == "VEVENT"]
    added_events = load_added_events()
    newly_added_events = set()

    for comp in user_cal.walk():
        if comp.name != "VEVENT":
            continue

        summary = comp.get('summary')
        if not summary:
            continue

        # Filter out specific events
        if "BMA152" in summary or "[BMA052 HT24]" in summary or "[BMA201 VT25]" in summary:
            continue

        title = extract_lecture_title(summary)
        dtstart_field = comp.get('dtstart')
        if not dtstart_field:
            continue

        if isinstance(dtstart_field.dt, datetime.datetime):
            new_dtstart = dtstart_field.dt
            dtend_field = comp.get('dtend')
            new_dtend = dtend_field.dt if dtend_field else new_dtstart + datetime.timedelta(hours=1)
        else:
            times = find_schema_times(comp, schema_events)
            if times is None:
                date_obj = dtstart_field.dt
                new_dtstart = datetime.datetime.combine(date_obj, datetime.time(23, 0))
                new_dtend = datetime.datetime.combine(date_obj, datetime.time(23, 59))
            else:
                new_dtstart, new_dtend = times

        new_title = adjust_zoom_title(title, comp)
        event_id = generate_event_id(comp)

        if event_id not in added_events:
            try:
                task = api.add_task(
                    content=new_title,
                    due_string=new_dtstart.isoformat(), # Use due_string for date and time
                    description=comp.get('location', '') + "\n" + comp.get('description', ''),
                )
                newly_added_events.add(event_id)
                print(f"Added task: {new_title} ({new_dtstart.isoformat()}) - Task ID: {task.id}")
            except Exception as e:
                print(f"Error adding task to Todoist: {e}")
        else:
            print(f"Skipping already added event: {new_title} ({new_dtstart.isoformat()})")

    # Update the list of added events
    added_events.update(newly_added_events)
    save_added_events(added_events)
    print("Calendar sync to Todoist completed.")

if __name__ == "__main__":
    sync_calendar_to_todoist()