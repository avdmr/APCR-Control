# presets.py
# Based directly on the POC implementation with minimal integration changes

import json
import os
import time
import threading
import struct
import socket
import queue
from datetime import datetime
import logging
import math
import controls

# For direct packet sending (since we can't access main.py's send_apcr_command)
_udp_socket = None

def _ensure_udp_socket():
    """Initialize UDP socket if needed"""
    global _udp_socket
    if _udp_socket is None:
        try:
            _udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            logging.info("Created UDP socket for preset movement")
        except Exception as e:
            logging.error(f"Failed to create UDP socket: {e}")
    return _udp_socket is not None

def _send_command(apcr, data):
    """Send a command to the APCR device"""
    if not _ensure_udp_socket():
        logging.error("No valid UDP socket available")
        return False
    
    try:
        ip = apcr['ip']
        port = 2390  # Standard port for APC-R
        _udp_socket.sendto(data, (ip, port))
        logging.debug(f"Sent packet to {ip}:{port}: {data.hex()}")
        return True
    except Exception as e:
        logging.error(f"Failed to send command: {e}")
        return False

# =============================================================================
# Config & Constants (exactly as in POC)
# =============================================================================

PRESETS_FILE = "presets.json"
BUFFER_SIZE = 1024

POSITION_REQUEST_FREQUENCY = 50  # 50x/sec (~20ms)

# Easing Parameters
EASE_MARGIN = 1.0
EASE_PERIOD = 20

ZOOM_EASE_MARGIN = 5.0   # abs position
ZOOM_EASE_PERIOD = 25    # percentage
ZOOM_EASE_SPEED_PCT = 3  # percentage
ZOOM_EASE_MIN_THRESHOLD = 30.0  # abs position

MAX_STEP_SIZE_PAN = 140.0
MAX_STEP_SIZE_TILT = 100.0
MAX_STEP_SIZE_ROLL = 50.0
MAX_ZOOM_SPEED = 225.0
MIN_ZOOM_SPEED = 25.0 
MIN_ZOOM_STEP = 0.1 
ZOOM_RANGE_MAX = 4095.0
ZOOM_TIMEOUT_THRESHOLD = 0.1
TIMEOUT_THRESHOLD = 0.1
ABS_ZOOM_THRESHOLD = 5

# Global state tracking
_recall_active = False
_recall_lock = threading.Lock()
_localPan = 0.0  # To match POC tracking

# =============================================================================
# Helper Functions (exactly as in POC)
# =============================================================================


def norm360(x):
    """Normaliseer een hoek zodat deze tussen 0 en 360° ligt."""
    return x % 360.0



def clamp(val, mn, mx):
    return max(mn, min(val, mx))

def to_little_endian_signed(val):
    return struct.pack('<h', val)

def native_to_normalized(angle):
    """
    Convert from gimbal's native coordinate system (-1800 to +1800 tenths)
    to normalized 0-360 degrees space.
    
    Args:
        angle: Angle in native system (degrees or tenths)
        
    Returns:
        float: Angle in 0-360 degrees space
    """
    # First convert to degrees if in tenths
    angle_deg = angle / 10.0 if abs(angle) > 180 else angle
    
    # Map the range [-180, 180] to [0, 360]
    if angle_deg >= 0:
        normalized = angle_deg
    else:
        normalized = 360 + angle_deg
        
    return normalized

def normalized_to_native(angle):
    """
    Convert from normalized 0-360 degrees space
    back to gimbal's native coordinate system.
    
    Args:
        angle: Angle in 0-360 degrees space
        
    Returns:
        float: Angle in native system (-180 to +180 degrees)
    """
    # Map [0, 360] back to [-180, 180]
    if angle <= 180:
        native = angle
    else:
        native = angle - 360
        
    return native

def inWall(angle, w1, w2):
    """
    Determine if an angle is inside the virtual wall in normalized 0-360 space.
    
    Args:
        angle: Angle to check (native scale, degrees or tenths)
        w1, w2: Wall boundaries (native scale, degrees or tenths)
        
    Returns:
        bool: True if angle is inside wall
    """
    if w1 is None or w2 is None:
        return False
    
    # Convert everything to the normalized 0-360 space
    angle_norm = native_to_normalized(angle)
    w1_norm = native_to_normalized(w1)
    w2_norm = native_to_normalized(w2)
    
    # Get the wall boundaries in the correct order
    if w1_norm <= w2_norm:
        wall_start = w1_norm
        wall_end = w2_norm
    else:
        wall_start = w2_norm
        wall_end = w1_norm
    
    # Check if angle is inside wall
    result = False
    
    # Handle wall that doesn't cross the 0/360 boundary
    if wall_start <= wall_end:
        result = wall_start <= angle_norm <= wall_end
    # Handle wall that crosses the 0/360 boundary
    else:
        result = angle_norm >= wall_start or angle_norm <= wall_end
    
    logging.debug(f"inWall: native={angle} (norm={angle_norm}°), wall=[{wall_start}°, {wall_end}°] => {'IN WALL' if result else 'OUTSIDE WALL'}")
    return result

def sample_arc_normalized(start_norm, end_norm, direction, step=5.0):
    """
    Generate a list of points along the arc in normalized 0-360 space.
    
    Args:
        start_norm: Start angle in normalized 0-360 space
        end_norm: End angle in normalized 0-360 space
        direction: 1 for CCW, -1 for CW
        step: Sampling step in degrees
        
    Returns:
        list: Points along the arc in normalized 0-360 space
    """
    points = [start_norm]
    
    if direction == 1:  # CCW (left)
        if end_norm > start_norm:
            # Simple CCW case without 0/360 
            current = start_norm
            while current < end_norm:
                current = min(current + step, end_norm)
                points.append(current)
        else:
            # CCW crossing 0/360 boundary
            current = start_norm
            # First go up to 360
            while current < 360:
                current = min(current + step, 360)
                points.append(current % 360)
            # Then from 0 to end
            current = 0
            while current < end_norm:
                current = min(current + step, end_norm)
                points.append(current)
    else:  # CW (negatieve richting, kleinere hoek)
        if end_norm < start_norm:
            # Simple CW case zonder 0/360 kruisen
            current = start_norm
            while current > end_norm:
                current = max(current - step, end_norm)
                points.append(current)
        else:
            # CW crossing 0/360 boundary
            current = start_norm
            # First go down to 0
            while current > 0:
                current = max(current - step, 0)
                points.append(current)
            # Then from 360 to end
            current = 360
            while current > end_norm:
                current = max(current - step, end_norm)
                points.append(current % 360)
    
    # Ensure end point is included if not already
    if points[-1] != end_norm:
        points.append(end_norm)
    
    return points

def rotation_crosses_wall(start, end, direction, w1, w2):
    """
    Check if the rotation from start to end in given direction crosses the virtual wall.
    Works in normalized 0-360 space.
    
    Args:
        start: Start angle in native system
        end: End angle in native system
        direction: 1 for CCW, -1 for CW
        w1, w2: Wall boundaries in native system
        
    Returns:
        bool: True if rotation crosses the wall
    """
    if w1 is None or w2 is None:
        return False
    
    # Convert to normalized 0-360 space
    start_norm = native_to_normalized(start)
    end_norm = native_to_normalized(end)
    w1_norm = native_to_normalized(w1)
    w2_norm = native_to_normalized(w2)
    
    # Get wall boundaries in order
    if w1_norm <= w2_norm:
        wall_start = w1_norm
        wall_end = w2_norm
    else:
        wall_start = w2_norm
        wall_end = w1_norm
    
    logging.debug(f"Testing path from {start_norm:.1f}° to {end_norm:.1f}° in {'CCW' if direction==1 else 'CW'} direction")
    logging.debug(f"Wall boundaries (normalized): [{wall_start:.1f}°, {wall_end:.1f}°]")
    
    # Gebruik een kleinere stapgrootte voor nauwkeurigere detectie
    step = 2.0  # Kleinere stap voor betere nauwkeurigheid
    
    # Sample points along the path in normalized space
    sample_points = sample_arc_normalized(start_norm, end_norm, direction, step)
    
    # Check if any point is inside the wall
    wall_crossed = False
    crossing_point = None
    
    for i in range(len(sample_points) - 1):
        pt = sample_points[i]
        next_pt = sample_points[i + 1]
        
        # Check if current point or next point is in wall
        in_wall_current = False
        in_wall_next = False
        
        if wall_start <= wall_end:  # Wall doesn't cross 0/360
            in_wall_current = wall_start <= pt <= wall_end
            in_wall_next = wall_start <= next_pt <= wall_end
        else:  # Wall crosses 0/360
            in_wall_current = pt >= wall_start or pt <= wall_end
            in_wall_next = next_pt >= wall_start or next_pt <= wall_end
        
        # Als een van de punten in de muur is, of als we de muur kruisen tussen punten
        if in_wall_current or in_wall_next:
            # Markeer het eerste punt dat de muur kruist
            crossing_point = pt if in_wall_current else next_pt
            wall_crossed = True
            break
    
    if wall_crossed:
        logging.debug(f"Path crosses wall at {crossing_point:.1f}° (normalized) inside wall [{wall_start:.1f}°, {wall_end:.1f}°]")
    else:
        logging.debug(f"Path from {start_norm:.1f}° to {end_norm:.1f}° in {'CCW' if direction==1 else 'CW'} direction does NOT cross wall")
    
    return wall_crossed

def calculate_shortest_rotation(a, b):
    """
    Simple function to calculate the shortest rotation from a to b.
    Returns (distance, direction) where direction is 1 for CCW (right) and -1 for CW (left).
    """
    # Convert to degrees if in tenths
    a_deg = a / 10.0 if abs(a) > 359 else a
    b_deg = b / 10.0 if abs(b) > 359 else b
    
    # Direct calculation of the rotation needed
    rotation = b_deg - a_deg
    
    # Normalize to ensure we get the shortest path
    if rotation > 180:
        rotation -= 360
    elif rotation < -180:
        rotation += 360
    
    # Determine direction based on the sign of the rotation
    if rotation >= 0:
        direction = 1  # CCW (right)
    else:
        direction = -1  # CW (left)
    
    logging.info(f"Simple shortest rotation: {abs(rotation):.1f}° in {'CCW (right)' if direction==1 else 'CW (left)'} direction")
    return (rotation, direction)

def shortest_rotation(a, b, apcr=None, settings=None):
    """
    Calculate the shortest rotation from position a to position b on the cyclic axis (in degrees),
    avoiding the virtual wall if possible when virtualwallpreset is enabled.
    
    In this implementation:
    - Positive rotation (CCW) = rightward movement
    - Negative rotation (CW) = leftward movement
    
    This is opposite to the conventional definition but consistent
    with how the APC-R interprets these movements.
    
    Args:
        a: Start angle in degrees
        b: End angle in degrees
        apcr: APC-R configuratie met virtual wall instellingen
        settings: Settings dictionary to check virtualwallpreset value
        
    Returns:
         (distance, direction)
         distance: total rotation in degrees (positive or negative)
         direction: 1 for right (CCW), -1 for left (CW)
         If movement is not possible without crossing the wall, returns (None, None)
    """
    # Check if virtual wall is disabled via settings
    virtualwall_enabled = False
    if settings and "global_settings" in settings:
        virtualwall_enabled = settings["global_settings"].get("virtualwallpreset", False)

    # If virtual wall is disabled, use simple shortest path calculation
    if not virtualwall_enabled:
        logging.info("Virtual wall preset disabled. Using shortest path.")
        return calculate_shortest_rotation(a, b)
    
    # Get camera-specific virtual wall settings
    virtualwallstart = None
    virtualwallend = None
    
    if apcr:
        virtualwallstart = apcr.get("virtualwallstart_pan")
        virtualwallend = apcr.get("virtualwallend_pan")
    
    # Only continue with virtual wall logic if enabled and boundaries are provided
    if virtualwallstart is None or virtualwallend is None:
        logging.info("Virtual wall enabled but no boundaries provided for this camera. Using shortest path.")
        return calculate_shortest_rotation(a, b)
        
    # Continue with the existing virtual wall logic
    # Convert pan values to degrees if they're in tenths of degrees
    a_deg = a / 10.0 if abs(a) > 359 else a
    b_deg = b / 10.0 if abs(b) > 359 else b
    wstart_deg = virtualwallstart / 10.0 if abs(virtualwallstart) > 359 else virtualwallstart
    wend_deg = virtualwallend / 10.0 if abs(virtualwallend) > 359 else virtualwallend
    
    a_norm = norm360(a_deg)
    b_norm = norm360(b_deg)

    # Calculate the two possible rotations (but don't decide yet)
    # Clockwise (CW) = negative rotation = leftward movement
    cw_rot = (a_norm - b_norm) % 360.0
    if cw_rot > 180:
        cw_rot = cw_rot - 360
    cw_rot = -cw_rot  # CW rotation is negative

    # Counterclockwise (CCW) = positive rotation = rightward movement
    ccw_rot = (b_norm - a_norm) % 360.0
    if ccw_rot > 180:
        ccw_rot = ccw_rot - 360
    
    # Find theoretical shortest route (for reference)
    shortest_option = cw_rot if abs(cw_rot) <= abs(ccw_rot) else ccw_rot
    shortest_dir = -1 if shortest_option == cw_rot else 1  # -1 for CW (left), 1 for CCW (right)

    # Check if target is in wall (not allowed)
    if inWall(b_deg, wstart_deg, wend_deg):
        logging.error(f"Preset endpoint {b_norm:.1f}° is inside the virtual wall area. Rotation not allowed.")
        return (None, None)
        
    # If start is in wall, find shortest exit path
    if inWall(a_deg, wstart_deg, wend_deg):
        w1 = norm360(wstart_deg)
        w2 = norm360(wend_deg)
        lower = min(w1, w2)
        upper = max(w1, w2)
        
        # Determine which "zone" of the wall we're in
        if w1 <= w2:  # Wall doesn't cross 0/360 boundary
            logging.debug(f"Wall doesn't cross 0/360 boundary: {w1}° to {w2}°")
            is_at_lower_half = (a_norm - lower) < (upper - a_norm)
        else:  # Wall crosses 0/360 boundary
            logging.debug(f"Wall crosses 0/360 boundary: {w1}° to {w2}°")
            # If a_norm is between lower and 360 OR between 0 and upper, determine which half
            if a_norm >= lower:  # Between lower and 360
                is_at_lower_half = (a_norm - lower) < (360 - a_norm + upper)
            else:  # Between 0 and upper
                is_at_lower_half = (360 - lower + a_norm) < (upper - a_norm)
        
        # Calculate exit point based on closest boundary
        if is_at_lower_half:
            # Exit via lower boundary (1° beyond)
            exit_pt = norm360(lower - 1)  # 1° outside the lower boundary
            exit_dir = -1  # CW direction (left)
            # Calculate rotation from a_norm to exit_pt
            exit_dist = (a_norm - exit_pt) % 360
            if exit_dist > 180:
                exit_dist = exit_dist - 360
            exit_dist = -exit_dist  # CW rotation is negative
        else:
            # Exit via upper boundary (1° beyond)
            exit_pt = norm360(upper + 1)  # 1° outside the upper boundary
            exit_dir = 1  # CCW direction (right)
            # Calculate rotation from a_norm to exit_pt
            exit_dist = (exit_pt - a_norm) % 360
            if exit_dist > 180:
                exit_dist = exit_dist - 360

        logging.info(f"Start point {a_norm:.1f}° is within wall. Exit chosen: {exit_dist:.1f}° in {'CCW (right)' if exit_dir==1 else 'CW (left)'} direction to {exit_pt:.1f}°.")
        
        # Maintain same direction for the entire route
        if exit_dir == 1:  # CCW (right)
            # Calculate CCW rotation from exit_pt to b_norm
            # This must ALWAYS be a positive rotation
            if b_norm >= exit_pt:
                remaining_dist = b_norm - exit_pt
            else:
                remaining_dist = 360 - exit_pt + b_norm
        else:  # CW (left)
            # Calculate CW rotation from exit_pt to b_norm
            # This must ALWAYS be a negative rotation
            if exit_pt >= b_norm:
                remaining_dist = -(exit_pt - b_norm)
            else:
                remaining_dist = -(360 - b_norm + exit_pt)
                
        # Total rotation = exit_rotation + remaining_rotation
        total_rot = exit_dist + remaining_dist

        logging.info(f"Total rotation: exit {exit_dist:.1f}° + remaining {remaining_dist:.1f}° = {total_rot:.1f}° in {'CCW (right)' if exit_dir==1 else 'CW (left)'} direction")
        
        return (total_rot, exit_dir)
    
    # Now test if either route crosses the wall
    cw_cross = rotation_crosses_wall(a_deg, b_deg, -1, wstart_deg, wend_deg)  # CW route (left)
    ccw_cross = rotation_crosses_wall(a_deg, b_deg, 1, wstart_deg, wend_deg)  # CCW route (right)

    # Simplified decision logic based on wall crossing
    if cw_cross and ccw_cross:
        logging.error(f"Both routes cross the wall. No safe route available.")
        return (None, None)
    elif cw_cross:
        # CW crosses wall, so use CCW
        chosen = ccw_rot
        overall_direction = 1  # CCW direction (right)
        logging.debug(f"CW route ({cw_rot:.1f}°) crosses wall; CCW route selected: {ccw_rot:.1f}°")
    elif ccw_cross:
        # CCW crosses wall, so use CW
        chosen = cw_rot
        overall_direction = -1  # CW direction (left)
        logging.debug(f"CCW route ({ccw_rot:.1f}°) crosses wall; CW route selected: {cw_rot:.1f}°")
    else:
        # Neither crosses wall, choose shortest
        chosen = shortest_option
        overall_direction = shortest_dir
        logging.debug(f"Both routes safe. Selected: shortest route {chosen:.1f}°")
        
    return (chosen, overall_direction)

# =============================================================================
# Command Building Functions (from POC)
# =============================================================================

def build_pan_tilt_roll_packet(camid, axis, degrees, relative=True):
    CMD_IDS = {'pan': '06', 'tilt': '07', 'roll': '08'}
    SCALES  = {'pan': 13.48, 'tilt': 13.365, 'roll': 13.365}
    if axis not in CMD_IDS:
        logging.error(f"Unknown axis {axis}")
        return None

    sval = int(round(degrees * SCALES[axis]))
    sval = clamp(sval, -32768, 32767)
    lehex = sval.to_bytes(2, byteorder='little', signed=True).hex()
    ctrl = '80' if relative else '81'
    packet = f"0A{camid:02X}06{'0'*5}E{CMD_IDS[axis]}{ctrl}00{lehex}"
    logging.debug(f"build_pan_tilt_roll_packet: {axis}={degrees}°, Packet={packet}")
    return packet

def build_relative_zoom_packet(camid, speed_int):
    speed_int = clamp(speed_int, -32768, 32767)
    lohi = to_little_endian_signed(speed_int).hex()
    packet = f"0A{camid:02X}06{'0'*6}098000{lohi}"
    logging.debug(f"build_relative_zoom_packet: speed_int={speed_int}, Packet={packet}")
    return packet

def build_absolute_zoom_packet(camid, zoom_val):
    z = clamp(int(round(zoom_val)), 0, 4095)
    lo = z & 0xFF
    hi = (z >> 8) & 0xFF
    packet = f"0A{camid:02X}0600000E0A80" + f"00{lo:02X}{hi:02X}"
    logging.debug(f"build_absolute_zoom_packet: zoom_val={zoom_val}, Packet={packet}")
    return packet

# =============================================================================
# Preset Management Functions
# =============================================================================

def load_presets():
    """Load presets from file"""
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                data = json.load(f)
            if 'presets' not in data:
                data['presets'] = {}
            logging.info(f"Loaded {len(data['presets'])} presets from {PRESETS_FILE}")
            return data
        else:
            return {'presets': {}}
    except Exception as e:
        logging.error(f"Error loading presets: {e}")
        return {'presets': {}}

def save_presets(presets_data):
    """Save presets to file"""
    try:
        with open(PRESETS_FILE, 'w') as f:
            json.dump(presets_data, f, indent=4)
        logging.info(f"Saved presets to {PRESETS_FILE}")
        return True
    except Exception as e:
        logging.error(f"Error saving presets: {e}")
        return False

def handle_feedback_pan(pan_degrees):
    """
    Updates the local pan tracking when receiving feedback.
    This function is called from controls.py when FDB messages are received.
    """
    global _localPan
    _localPan = pan_degrees
    logging.debug(f"handle_feedback_pan: New localPan set to {_localPan:.1f}°")

def save_preset(camid, slot_num, position, mapped_button=None, device_id=None):
    """
    Save current position to a preset slot.
    Converts position values from tenths of degrees to degrees before storing.
    
    Args:
        camid: Camera ID
        slot_num: Slot number
        position: Position dictionary or string with FDB data
        mapped_button: Optional button mapping
        device_id: Optional device ID for button mapping
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Validate inputs
        if camid is None:
            logging.error("Cannot save preset: camid is None")
            return False
            
        if slot_num is None or not isinstance(slot_num, int) or slot_num < 1:
            logging.error(f"Cannot save preset: invalid slot_num {slot_num}")
            return False
            
        if position is None:
            logging.error("Cannot save preset: position is None")
            return False
        
        presets_data = load_presets()
        
        # Process position - either use a dictionary or parse FDB string
        if isinstance(position, dict) and all(k in position for k in ['pan', 'tilt', 'roll', 'zoom']):
            # Extract raw values
            pan_val = float(position['pan'])
            tilt_val = float(position['tilt'])
            roll_val = float(position['roll'])
            zoom_val = float(position['zoom'])
        elif isinstance(position, str) and position.startswith('FDB;'):
            parts = position.strip().split(';')
            if len(parts) >= 6:
                # Check if the camid in the FDB string matches the requested camid
                fdb_camid = int(parts[1])
                if fdb_camid != camid:
                    logging.warning(f"FDB camid ({fdb_camid}) doesn't match requested camid ({camid}). Using requested camid.")
                
                pan_val = float(parts[2])
                tilt_val = float(parts[3])
                roll_val = float(parts[4])
                zoom_val = float(parts[5])
            else:
                logging.error(f"Invalid FDB position data: {position}")
                return False
        else:
            logging.error(f"Invalid position data format: {position}")
            return False
        
        # Always convert pan, tilt, and roll from tenths of degrees to degrees
        # FDB messages always have these values in tenths of degrees
        pan_deg = pan_val / 10.0
        tilt_deg = tilt_val / 10.0
        roll_deg = roll_val / 10.0
        
        # Log the conversion for clarity
        logging.debug(f"Converting position values: pan {pan_val} -> {pan_deg}°, tilt {tilt_val} -> {tilt_deg}°, roll {roll_val} -> {roll_deg}°")
        
        # Create the preset entry with converted values
        preset_key = f"{camid}.{slot_num}"
        
        # Check if this preset already exists to preserve mapped_buttons
        if preset_key in presets_data['presets']:
            mapped_buttons = presets_data['presets'][preset_key].get('mapped_buttons', {})
        else:
            mapped_buttons = {}
        
        # Update button mapping if provided
        if mapped_button is not None and device_id is not None:
            mapped_buttons[device_id] = mapped_button
        
        # Create/update the preset entry
        presets_data['presets'][preset_key] = {
            'pan': pan_deg,
            'tilt': tilt_deg,
            'roll': roll_deg,
            'zoom': zoom_val,  # Zoom doesn't need conversion
            'mapped_buttons': mapped_buttons
        }
        
        # Save to file
        result = save_presets(presets_data)
        if result:
            logging.info(f"Saved preset for {preset_key}: pan={pan_deg}°, tilt={tilt_deg}°, roll={roll_deg}°, zoom={zoom_val}")
        
        return result
        
    except Exception as e:
        logging.error(f"Error saving preset: {e}")
        return False

def get_preset(camid, slot_num):
    """
    Get a preset by camid and slot number
    
    Args:
        camid: Camera ID
        slot_num: Slot number
        
    Returns:
        dict: Preset data or None if not found
    """
    try:
        # Validate inputs
        if camid is None:
            logging.error("Cannot get preset: camid is None")
            return None
            
        if slot_num is None or not isinstance(slot_num, int) or slot_num < 1:
            logging.error(f"Cannot get preset: invalid slot_num {slot_num}")
            return None
        
        presets_data = load_presets()
        preset_key = f"{camid}.{slot_num}"
        
        if preset_key in presets_data['presets']:
            preset = presets_data['presets'][preset_key]
            return {
                'position': preset,
                'mapped_buttons': preset.get('mapped_buttons', {})
            }
        else:
            logging.warning(f"Preset {preset_key} not found")
            return None
    except Exception as e:
        logging.error(f"Error getting preset: {e}")
        return None


def list_presets(camid=None):
    """
    List all presets, optionally filtered by camid
    
    Args:
        camid: Optional camera ID to filter by
        
    Returns:
        dict: Presets indexed by key
    """
    try:
        presets_data = load_presets()
        
        if camid is None:
            return presets_data['presets']
        
        filtered = {}
        prefix = f"{camid}."
        for key, value in presets_data['presets'].items():
            if key.startswith(prefix):
                filtered[key] = value
        
        return filtered
    except Exception as e:
        logging.error(f"Error listing presets: {e}")
        return {}

def delete_preset(camid, slot_num):
    """Delete a preset"""
    try:
        presets_data = load_presets()
        preset_key = f"{camid}.{slot_num}"
        
        if preset_key in presets_data['presets']:
            del presets_data['presets'][preset_key]
            
            result = save_presets(presets_data)
            if result:
                logging.info(f"Deleted preset {preset_key}")
            
            return result
        else:
            logging.warning(f"Preset {preset_key} not found, nothing to delete")
            return False
    except Exception as e:
        logging.error(f"Error deleting preset: {e}")
        return False

def delete_all_presets():
    """Delete all presets"""
    try:
        result = save_presets({'presets': {}})
        if result:
            logging.info("Deleted all presets")
        return result
    except Exception as e:
        logging.error(f"Error deleting all presets: {e}")
        return False

def is_recall_active():
    """
    Check if a preset recall is currently active
    
    Returns:
        bool: True if a recall is in progress
    """
    global _recall_active
    return _recall_active

def stop_recall():
    """
    Stop any ongoing preset recall operation
    """
    global _recall_active
    with _recall_lock:
        _recall_active = False
    logging.info("Preset recall operation stopped")

def inWall(angle, w1, w2):
    """
    Determine if an angle is inside the virtual wall in normalized 0-360 space.
    
    Args:
        angle: Angle to check (native scale, degrees or tenths)
        w1, w2: Wall boundaries (native scale, degrees or tenths)
        
    Returns:
        bool: True if angle is inside wall
    """
    if w1 is None or w2 is None:
        return False
    
    # Convert everything to the normalized 0-360 space
    angle_norm = native_to_normalized(angle)
    w1_norm = native_to_normalized(w1)
    w2_norm = native_to_normalized(w2)
    
    # Get the wall boundaries in the correct order
    if w1_norm <= w2_norm:
        wall_start = w1_norm
        wall_end = w2_norm
    else:
        wall_start = w2_norm
        wall_end = w1_norm
    
    # Check if angle is inside wall
    result = False
    
    # Handle wall that doesn't cross the 0/360 boundary
    if wall_start <= wall_end:
        result = wall_start <= angle_norm <= wall_end
    # Handle wall that crosses the 0/360 boundary
    else:
        result = angle_norm >= wall_start or angle_norm <= wall_end
    
    logging.debug(f"inWall: native={angle} (norm={angle_norm}°), wall=[{wall_start}°, {wall_end}°] => {'IN WALL' if result else 'OUTSIDE WALL'}")
    return result

def native_to_normalized(angle):
    """
    Convert from gimbal's native coordinate system (-1800 to +1800 tenths)
    to normalized 0-360 degrees space.
    
    Args:
        angle: Angle in native system (degrees or tenths)
        
    Returns:
        float: Angle in 0-360 degrees space
    """
    # First convert to degrees if in tenths
    angle_deg = angle / 10.0 if abs(angle) > 359 else angle
    
    # Map the range [-180, 180] to [0, 360]
    normalized = angle_deg % 360.0
    
    return normalized

# =============================================================================
# Main Preset Recall Function
# =============================================================================

def recall_preset(camid, slot_num, apcr, settings):
    """
    Direct adaptation of the POC's recall_position method but integrated with
    the existing controls system.
    
    Args:
        camid: Camera ID to recall preset for
        slot_num: Preset slot number
        apcr: APC-R configuration dictionary
        settings: Application settings dictionary
    """
    global _recall_active, _localPan
    
    # Verify that the provided apcr matches the camid
    if apcr.get('camid') != camid:
        logging.error(f"Mismatch between requested camid ({camid}) and provided APC-R ({apcr.get('camid')})")
        print(f"[ERROR] Cannot recall preset: Requested preset for CamID {camid} but provided APC-R has CamID {apcr.get('camid')}")
        return
    
    with _recall_lock:
        # Check if another recall is already in progress
        if _recall_active:
            logging.warning("A preset recall is already in progress. Ignoring this request.")
            print("[WARNING] A preset recall is already in progress.")
            return
        _recall_active = False  # Temporarily set to False for internal initialization
    
    try:
        # Reset virtual wall tracker at the start of each recall
        # This ensures each movement starts with a clean slate
        if hasattr(controls, 'virtual_wall_tracker'):
            controls.virtual_wall_tracker["pan"] = None
            controls.virtual_wall_tracker["tilt"] = None
            logging.debug("Reset virtual wall tracker at start of recall")
        
        # Now we're ready for initialization, set the active flag to True
        with _recall_lock:
            _recall_active = True
        
        # Get preset data
        preset_key = f"{camid}.{slot_num}"
        preset_data = get_preset(camid, slot_num)
        
        if not preset_data:
            logging.error(f"No preset found for {preset_key}")
            print(f"[ERROR] No preset found for {preset_key}")
            with _recall_lock:
                _recall_active = False
            return
        
        # Extract target values from preset
        tg = preset_data['position']
        
        # Get preset speed from settings
        sp = settings["global_settings"].get("preset_transition_speed", 50)
        
        # Get virtual wall settings from the specific camera settings
        virtualwall_active = settings["global_settings"].get("virtualwallpreset", False)
        
        if virtualwall_active:
            # Haal virtual wall instellingen van de specifieke camera op
            wstart = apcr.get("virtualwallstart_pan")
            wend = apcr.get("virtualwallend_pan")
            logging.info(f"Virtual Wall active for {apcr['name']}: start={wstart}, end={wend}")
            
            # First check: Is target position inside virtual wall?
            if wstart is not None and wend is not None:
                in_wall = inWall(tg['pan'], wstart, wend)
                logging.debug(f"Testing if target {tg['pan']} is inside wall: {in_wall}")
                if in_wall:
                    logging.error(f"Target position {tg['pan']} is inside virtual wall range ({wstart}-{wend}). Movement not allowed.")
                    print(f"[ERROR] Target position for preset {preset_key} is inside virtual wall range. Movement not allowed.")
                    with _recall_lock:
                        _recall_active = False
                    return
        else:
            wstart = None
            wend = None
            logging.info("Virtual Wall disabled for presets")
        
        logging.info(f"Recall => {preset_key}, speed={sp}% => {tg}")
        print(f"[INFO] Recalling preset {preset_key} for {apcr['name']} (CamID {camid}) at speed {sp}%")
        
        # Store original position request frequency and change to high frequency
        original_freq = settings["global_settings"].get("position_request_frequency", 1.2)
        settings["global_settings"]["position_request_frequency"] = 0.05  # 20 requests per second
        
        # Wait a bit to let the new frequency take effect
        time.sleep(0.2)
        
        # Send a few position requests directly to be up-to-date
        for _ in range(3):
            camid_hex = f"{apcr['camid']:02x}"
            data_hex = "08" + camid_hex + "0400000e140000"
            data = bytes.fromhex(data_hex)
            _send_command(apcr, data)  # Use _send_command for internal communication
            time.sleep(0.04)  # Small wait time between requests
        
        # Get current position
        cpos_str = controls.get_current_position(apcr)
        if not cpos_str:
            logging.error(f"No current position available for {apcr['name']} (CamID {camid}) => can't recall.")
            print(f"[ERROR] No current position available for {apcr['name']} => can't recall preset.")
            with _recall_lock:
                _recall_active = False
            settings["global_settings"]["position_request_frequency"] = original_freq
            return
        
        parts = cpos_str.strip().split(';')
        if len(parts) < 6:
            logging.error(f"Invalid position data: {cpos_str}")
            print(f"[ERROR] Invalid position data received: {cpos_str}")
            with _recall_lock:
                _recall_active = False
            settings["global_settings"]["position_request_frequency"] = original_freq
            return
        
        # Update localPan from tracked value (which gets updated by handle_feedback_pan)
        cPan = _localPan
        cTilt = float(parts[3]) / 10.0  # Convert to degrees if in tenths
        cRoll = float(parts[4]) / 10.0  # Convert to degrees if in tenths
        cZoom = float(parts[5])
        
        # Current position logging (show both raw and normalized angles)
        cPan_norm = norm360(cPan)
        logging.debug(f"Current Position: Pan={cPan}° ({cPan_norm}° normalized), Tilt={cTilt}°, Roll={cRoll}°, Zoom={cZoom}")
        
        # Get target values from preset
        tPan = tg['pan']
        tTilt = tg['tilt']
        tRoll = tg['roll']
        tZoom = tg['zoom']
        
        # Target position logging (show both raw and normalized angles)
        tPan_deg = tPan / 10.0 if abs(tPan) > 359 else tPan
        tPan_norm = norm360(tPan_deg)
        logging.info(f"Target Position: Pan={tPan}° ({tPan_norm}° normalized), Tilt={tTilt}°, Roll={tRoll}°, Zoom={tZoom}")
        
        # Calculate shortest rotation with virtual wall - pass the apcr for camera-specific virtual wall settings
        distance, direction = shortest_rotation(
            a=cPan,
            b=tPan,
            apcr=apcr,
            settings=settings
        )
        
        if distance is None:
            logging.error("Movement to target position blocked by virtual wall.")
            print(f"[ERROR] Movement to target position for preset {preset_key} is blocked by virtual wall.")
            with _recall_lock:
                _recall_active = False
            settings["global_settings"]["position_request_frequency"] = original_freq
            return
        
        # IMPROVED CALCULATION OF FINALPAN:
        finalPan = cPan + distance  # Simple addition, regardless of direction or value
            
        # Log the rotation information
        logging.debug(f"recall_preset => final raw pan={finalPan}°, normalized={norm360(finalPan)}°, distance={distance}°, direction={'CCW (right)' if direction==1 else 'CW (left)'}")
        
        # Do the move (with high-frequency position requests)
        do_move_segment(
            apcr=apcr,
            cPan=cPan, 
            cTilt=cTilt, 
            cRoll=cRoll, 
            cZoom=cZoom,
            tgtPan=finalPan, 
            tgtTilt=tTilt, 
            tgtRoll=tRoll, 
            tgtZoom=tZoom,
            speed=sp,
            direction=direction,
            settings=settings
        )
        
        # Restore original frequency
        settings["global_settings"]["position_request_frequency"] = original_freq
        
    except Exception as e:
        logging.error(f"Error in recall_preset: {e}")
        print(f"[ERROR] Exception during preset recall: {e}")
    
    finally:
        with _recall_lock:
            _recall_active = False
        logging.debug(f"Preset recall to {preset_key} completed")
        print(f"[INFO] Preset recall to {preset_key} completed")
# =============================================================================
# Movement Segment (directly from POC)
# =============================================================================

def do_move_segment(apcr, cPan, cTilt, cRoll, cZoom,
                   tgtPan, tgtTilt, tgtRoll, tgtZoom,
                   speed=50, direction=1, settings=None):
    """
    Execute a smooth movement from current position to target position.
    
    The route planning is done BEFORE this function is called in shortest_rotation().
    This function just executes the movement along the pre-planned route.
    """
    global _localPan
    
    logging.info(f"do_move_segment => from pan={cPan:.1f}° to {tgtPan:.1f}°, speed={speed}%")
    
    # Calculate step sizes based on speed
    st_pan_max = min((MAX_STEP_SIZE_PAN * speed) / 100.0, 90.0)
    st_tilt = (MAX_STEP_SIZE_TILT * speed) / 100.0
    st_roll = (MAX_STEP_SIZE_ROLL * speed) / 100.0
    zoom_speed_range = MAX_ZOOM_SPEED - MIN_ZOOM_SPEED
    st_zoom = MIN_ZOOM_SPEED + (zoom_speed_range * speed / 100.0)
    st_zoom = max(st_zoom, MIN_ZOOM_STEP)  # Zorg dat we nooit onder MIN_ZOOM_STEP komen
    
    # Easing parameters
    pan_ease_frac  = EASE_PERIOD / 100.0
    zoom_ease_frac = ZOOM_EASE_PERIOD / 100.0
    last_movement_time = time.time() 
    zoom_stable_time = None
    
    zEaseVal = int(round((ZOOM_EASE_SPEED_PCT / 100.0) * 1000.0))
    z_initDist = abs(tgtZoom - cZoom)
    use_sub = (z_initDist < ZOOM_EASE_MIN_THRESHOLD)
    
    # Create a stop event for local control
    stop_event = threading.Event()
    
    # Variabele om de laatste bewegingsrichting bij te houden
    last_dp_sign = None
    overshoot_count = 0  # Tel het aantal overshoot events

    # Main movement loop
    while not stop_event.is_set() and is_recall_active():
        # Calculate pan delta with direction consideration
        dp = (tgtPan - _localPan) % 360.0
        if dp > 180.0:
            dp -= 360.0
        
        # Bepaal de huidige richtingsteken
        current_dp_sign = 1 if dp >= 0 else -1
        
        # Detecteer richtingsverandering/overshoot met tolerantie EASE_MARGIN
        overshoot_detected = False
        if last_dp_sign is not None and current_dp_sign != last_dp_sign:    
            # Als de richting is veranderd en we redelijk dicht bij het doel zijn,
            # hebben we waarschijnlijk een overshoot
            if abs(dp) < (EASE_MARGIN * 5):  # Ruimere tolerantie voor overshoot detectie
                overshoot_detected = True
                overshoot_count += 1
                logging.info(f"Overshoot #{overshoot_count} detected: sign of dp changed from {last_dp_sign} to {current_dp_sign}, distance: {abs(dp):.2f}°")
        
        # Update last_dp_sign
        last_dp_sign = current_dp_sign
        
        # Controleer of virtual wall actief is
        virtual_wall_active = settings and settings.get("global_settings", {}).get("virtualwallpreset", False)
        
        # Dynamische aanpassing van stapgrootte op basis van afstand tot doel
        if abs(dp) < 20.0:
            # Bijna bij het doel - gebruik kleinere stappen voor meer precisie
            reduction_factor = max(0.3, min(1.0, abs(dp) / 20.0))
            p_st = st_pan_max * reduction_factor
        else:
            # Normale stapgrootte voor grotere afstanden
            p_st = st_pan_max
        
        # Richtingskeuze - alleen forceren als nodig
        if any([
            abs(dp) <= EASE_MARGIN * 3,  # Dicht bij doel - gebruik kortste route
            overshoot_detected,          # Bij overshoot - gebruik natuurlijke route
            overshoot_count >= 2,        # Bij herhaalde overshoots - gebruik natuurlijke route
            not virtual_wall_active      # Als virtual wall niet actief is - gebruik kortste route
        ]):
            # Gebruik de natuurlijke richting (kortste route) - geen aanpassing van dp
            pass
        else:
            # Forceer de richting zoals bepaald in de routeplanning
            if (direction == 1 and dp < 0) or (direction == -1 and dp > 0):
                dp = -dp
        
        # Bereken deltas voor andere assen
        dt = tgtTilt - cTilt
        dr = tgtRoll - cRoll
        dz = tgtZoom - cZoom
        
        # Check if all axes are close to target
        doneP = (abs(dp) <= EASE_MARGIN)
        doneT = (abs(dt) <= EASE_MARGIN)
        doneR = (abs(dr) <= EASE_MARGIN)
        doneZ = (abs(dz) <= ZOOM_EASE_MARGIN)
        
        # Als alle assen dicht genoeg bij hun doelpositie zijn, beëindig de beweging
        if doneP and doneT and doneR and doneZ:
            logging.debug("Segment done => zoom idle + absolute jump if needed.")
            # Send idle command for zoom
            idle_packet_hex = controls.get_idle_packet('zoom', apcr['camid'])
            if idle_packet_hex:
                data = bytes.fromhex(idle_packet_hex)
                _send_command(apcr, data)
            
            time.sleep(0.2)
            
            # Send absolute zoom if needed
            if abs(dz) > 0.1:
                absZ = build_absolute_zoom_packet(apcr['camid'], tgtZoom)
                _send_command(apcr, bytes.fromhex(absZ))
                time.sleep(0.3)
            
            break
        
        # Calculate pan/tilt/roll easing
        mxptr = max(abs(dp), abs(dt), abs(dr))
        if mxptr < (pan_ease_frac * max(p_st, st_tilt, st_roll)):
            p_st = p_st * 0.5
            t_st = st_tilt * 0.5
            r_st = st_roll * 0.5
        else:
            # p_st wordt al eerder dynamisch aangepast
            t_st = st_tilt
            r_st = st_roll
        
        # Calculate zoom easing
        cDist = abs(dz)
        frac = cDist / z_initDist if z_initDist > 0 else 0.0
        boundary = 1.0 - zoom_ease_frac
        
        if use_sub:
            fraction = frac
            normalZoom = False
        else:
            if frac >= boundary:
                normalZoom = True
            else:
                normalZoom = False
                fraction = frac
        
        # Calculate zoom speed
        if normalZoom:
            needed = clamp(dz, -st_zoom, st_zoom)
            zspeed = int(round(needed))
            zspeed = clamp(zspeed, -1000, 1000)
        else:
            fractIn = (zoom_ease_frac - fraction) / zoom_ease_frac if zoom_ease_frac > 0 else 1.0
            fractIn = clamp(fractIn, 0, 1)
            
            if fractIn < 0.25:
                partial = fractIn / 0.25
                normNeeded = clamp(dz, -st_zoom, st_zoom)
                normS = clamp(int(round(normNeeded)), -1000, 1000)
                halfE = (zEaseVal // 2) if dz > 0 else -(zEaseVal // 2)
                zspeed = int((1 - partial) * normS + partial * halfE)
            elif fractIn < 0.50:
                partial = (fractIn - 0.25) / 0.25
                halfE = (zEaseVal // 2) if dz > 0 else -(zEaseVal // 2)
                thrQ = int(round(0.75 * zEaseVal)) if dz > 0 else -int(round(0.75 * zEaseVal))
                zspeed = int((1 - partial) * halfE + partial * thrQ)
            elif fractIn < 0.75:
                partial = (fractIn - 0.50) / 0.25
                thrQ = int(round(0.75 * zEaseVal)) if dz > 0 else -int(round(0.75 * zEaseVal))
                finE = zEaseVal if dz > 0 else -zEaseVal
                zspeed = int((1 - partial) * thrQ + partial * finE)
            else:
                zspeed = zEaseVal if dz > 0 else -zEaseVal
        
        # Calculate movement for each axis
        mv_p = clamp(dp, -p_st, p_st)
        mv_t = clamp(dt, -t_st, t_st)
        mv_r = clamp(dr, -r_st, r_st)
        mv_z = clamp(dz, -st_zoom, st_zoom)
        
        # Check for zoom stability timeout
        if abs(dz) <= ZOOM_EASE_MARGIN and abs(mv_p) <= 0.001 and abs(mv_t) <= 0.001 and abs(mv_r) <= 0.001:
            if zoom_stable_time is None:
                zoom_stable_time = time.time()
            elif time.time() - zoom_stable_time > ZOOM_TIMEOUT_THRESHOLD:
                logging.info("All axes stable for too long, forcing absolute zoom.")
                if abs(dz) <= ABS_ZOOM_THRESHOLD:
                    # Send zoom idle
                    idle_packet_hex = controls.get_idle_packet('zoom', apcr['camid'])
                    if idle_packet_hex:
                        data = bytes.fromhex(idle_packet_hex)
                        _send_command(apcr, data)
                    time.sleep(0.3)
                    
                    # Send absolute zoom
                    absZ = build_absolute_zoom_packet(apcr['camid'], tgtZoom)
                    _send_command(apcr, bytes.fromhex(absZ))
                    time.sleep(0.3)
                break
        else:
            zoom_stable_time = None
        
        # Check for zoom overshoot
        if (dz > 0 and mv_z < 0) or (dz < 0 and mv_z > 0):
            logging.warning("Zoom overshoot detected! Forcing absolute zoom correction.")
            # Send zoom idle
            idle_packet_hex = controls.get_idle_packet('zoom', apcr['camid'])
            if idle_packet_hex:
                data = bytes.fromhex(idle_packet_hex)
                _send_command(apcr, data)
            time.sleep(0.3)
            
            # Send absolute zoom
            absZ = build_absolute_zoom_packet(apcr['camid'], tgtZoom)
            _send_command(apcr, bytes.fromhex(absZ))
            time.sleep(0.3)
            break
        
        # Send movement commands for each axis
        if abs(mv_p) > 0.001:
            phex = build_pan_tilt_roll_packet(apcr['camid'], 'pan', mv_p, True)
            if phex:
                _send_command(apcr, bytes.fromhex(phex))
                logging.debug(f"Sent Pan Command: {phex}")
            time.sleep(0.02)
        
        if abs(mv_t) > 0.001:
            thex = build_pan_tilt_roll_packet(apcr['camid'], 'tilt', mv_t, True)
            if thex:
                _send_command(apcr, bytes.fromhex(thex))
                logging.debug(f"Sent Tilt Command: {thex}")
            time.sleep(0.02)
        
        if abs(mv_r) > 0.001:
            rhex = build_pan_tilt_roll_packet(apcr['camid'], 'roll', mv_r, True)
            if rhex:
                _send_command(apcr, bytes.fromhex(rhex))
                logging.debug(f"Sent Roll Command: {rhex}")
            time.sleep(0.02)
        
        if abs(mv_z) > 0.001:
            zpack = build_relative_zoom_packet(apcr['camid'], int(zspeed))
            _send_command(apcr, bytes.fromhex(zpack))
            logging.debug(f"Sent Zoom Command: {zpack}")
            time.sleep(0.02)
        else:
            # Send zoom idle if not sending a zoom command
            idle_packet_hex = controls.get_idle_packet('zoom', apcr['camid'])
            if idle_packet_hex:
                data = bytes.fromhex(idle_packet_hex)
                _send_command(apcr, data)
                logging.debug(f"Sent Zoom Idle Command")
            time.sleep(0.02)
        
        # Update last movement time if any axis moved
        if abs(mv_p) > 0.001 or abs(mv_t) > 0.001 or abs(mv_r) > 0.001 or abs(mv_z) > 0.001:
            last_movement_time = time.time()
        
        # Check for movement timeout
        if time.time() - last_movement_time > TIMEOUT_THRESHOLD:
            logging.warning("No movement detected for too long, stopping all movement.")
            for ax in ['pan', 'tilt', 'roll', 'zoom']:
                idle_packet_hex = controls.get_idle_packet(ax, apcr['camid'])
                if idle_packet_hex:
                    data = bytes.fromhex(idle_packet_hex)
                    _send_command(apcr, data)
                    logging.debug(f"Sent Idle Command for {ax}")
            break
        
        # Get updated position
        p2_str = controls.get_current_position(apcr)
        if not p2_str:
            logging.error("No new position data received => ending do_move_segment.")
            break
        
        # Parse updated position
        parts = p2_str.strip().split(';')
        if len(parts) >= 6:
            # localPan is updated by handle_feedback_pan, but we get other values here
            cTilt = float(parts[3]) / 10.0
            cRoll = float(parts[4]) / 10.0
            cZoom = float(parts[5])
            
            logging.debug(f"Updated position: Pan={_localPan:.1f}°, Tilt={cTilt:.1f}°, Roll={cRoll:.1f}°, Zoom={cZoom}")
        else:
            logging.warning(f"Invalid position data: {p2_str}")
    
    # Final idle commands to ensure movement stops
    for _ in range(3):
        for ax in ['pan', 'tilt', 'roll', 'zoom']:
            idle_packet_hex = controls.get_idle_packet(ax, apcr['camid'])
            if idle_packet_hex:
                data = bytes.fromhex(idle_packet_hex)
                _send_command(apcr, data)
                logging.debug(f"Sent final Idle Command for {ax}")
        time.sleep(0.05)
    
    logging.debug("do_move_segment completed.")


