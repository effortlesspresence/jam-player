from pathlib import Path

scenes_update_flag_file = Path(
    '/home/comitup/.jam/app_data/live_scenes_updated.txt'
)


def should_reload_scenes():
    """Check if scenes should be reloaded based on the update file."""
    try:
        if scenes_update_flag_file.exists():
            with open(scenes_update_flag_file, 'r') as f:
                content = f.read().strip()
                return bool(int(content))  # Convert to int then bool to handle various truthy values
        return False
    except (ValueError, IOError) as e:
        print(f"Error reading scenes update file: {e}")
        return False


def reset_update_flag_to_zero():
    """Reset the update flag to 0."""
    try:
        with open(scenes_update_flag_file, 'w') as f:
            f.write('0')
        print("Reset scenes update flag to 0")
    except IOError as e:
        print(f"Error setting scenes update flag to 0: {e}")


def reset_update_flag_to_one():
    """Reset the update flag to 1."""
    try:
        with open(scenes_update_flag_file, 'w') as f:
            f.write('1')
        print("Reset scenes update flag to 1")
    except IOError as e:
        print(f"Error setting scenes update flag to 1: {e}")