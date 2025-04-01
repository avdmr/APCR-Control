import os
import sys
import time
import threading
import queue
import tkinter as tk
from tkinter import scrolledtext, ttk
import ctypes
import logging
import pystray
import re
import pygame
from PIL import Image, ImageDraw, ImageTk

# --- Tracking van verbindingen ---
connected_camids = {}           # Dict van camid -> naam + status
disconnected_notified = set()   # Set van CAMIDs waarvoor al een disconnection melding is getoond
app_startup_phase = True        # Indicator dat de app in de opstartfase is
listener_ip = None              # Bijhouden van het gedetecteerde listener IP

# --- Gedeelde output queue voor alle output ---
output_queue = queue.Queue()

# --- Custom logging handler die output naar onze queue stuurt ---
class QueueHandler(logging.Handler):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue
        
    def emit(self, record):
        msg = self.format(record)
        self.queue.put(msg + '\n')

# --- Custom stdout/stderr redirection ---
class QueueStream:
    def __init__(self, q):
        self.q = q
    def write(self, msg):
        if msg and msg.strip():
            if not msg.endswith("\n"):
                msg += "\n"
            self.q.put(msg)
    def flush(self):
        pass

# --- Custom stdin redirection ---
class CustomStdin:
    def __init__(self):
        self.queue = queue.Queue()
        self.orig_stdin = sys.stdin
        
    def readline(self):
        # Blokkeer totdat er een regel beschikbaar is
        return self.queue.get() + '\n'
        
    def write(self, line):
        # Voeg een regel toe aan de stdin queue
        self.queue.put(line.rstrip('\n'))
        
    def flush(self):
        pass

# --- Configureer de logging voordat andere modules worden geïmporteerd ---
logging_handler = QueueHandler(output_queue)
logging_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logging_handler.setLevel(logging.INFO)

# Verwijder bestaande handlers en voeg onze eigen handler toe
root_logger = logging.getLogger()
root_logger.handlers = []
root_logger.addHandler(logging_handler)
root_logger.setLevel(logging.INFO)

# Redirect stdout/stderr
sys.stdout = QueueStream(output_queue)
sys.stderr = QueueStream(output_queue)

# Redirect stdin
custom_stdin = CustomStdin()
sys.stdin = custom_stdin

# Nu importeer de hoofdfunctionaliteit (nadat we alle redirects hebben ingesteld)
import main

# --- Basis app setup ---
logging.info("Starting APC-R Controller Tray Application")
logging.info(f"Python version: {sys.version}")
logging.info(f"Platform: {sys.platform}")
logging.info(f"Executable: {sys.executable}")
logging.info(f"Working directory: {os.getcwd()}")

# --- Configuratie ---
TITLE = "APC-R Controller"
ICON_PATH_ICO = "icon.ico"  # Zorg dat dit bestand aanwezig is
ICON_PATH_PNG = "icon.png"  # Alternatief icoon

# --- Globale variabelen ---
root = None          # Tkinter root window
app = None           # Instantie van de GUI-applicatie
tray_icon = None     # pystray icon
menu_state = "idle"  # Huidige menustatus (idle, main_menu, settings, mapping)
waiting_for_enter = False  # Als de app wacht op enter toetsen
event_loop_terminating = False  # De event loop is aan het afsluiten




# --- GUI verbeteringen voor mapping ---
def gui_wait_for_mapping_input(joystick):
    """
    GUI-friendly version of waiting for input mapping with buttons instead of commands.
    """
    # Create a dialog window
    dialog = tk.Toplevel(root)
    dialog.title("Map Input")
    dialog.geometry("400x300")
    dialog.transient(root)  # Make the dialog dependent on the main window
    dialog.grab_set()  # Make the dialog modal
    
    chosen_input = [None]  # Use a list to be able to modify the value in nested functions
    last_axis_values = [0.0] * joystick.get_numaxes()  # Store last axis values for comparison
    
    # Instructions
    tk.Label(dialog, text="Move the joystick or press a button to map this action").pack(pady=10)
    
    # Selection display
    selection_var = tk.StringVar(value="Nothing selected yet")
    tk.Label(dialog, textvariable=selection_var).pack(pady=20)
    
    # Button frame
    button_frame = tk.Frame(dialog)
    button_frame.pack(side=tk.BOTTOM, pady=20)
    
    # Confirm button (initially disabled)
    confirm_button = tk.Button(button_frame, text="Confirm", state=tk.DISABLED)
    confirm_button.pack(side=tk.LEFT, padx=10)
    
    # Cancel button
    cancel_button = tk.Button(button_frame, text="Cancel")
    cancel_button.pack(side=tk.LEFT, padx=10)
    
    # Update function for input detection
    def update_detection():
        pygame.event.pump()
        
        # Check axes
        for axis_idx in range(joystick.get_numaxes()):
            value = joystick.get_axis(axis_idx)
            if abs(value - last_axis_values[axis_idx]) > 0.01:
                last_axis_values[axis_idx] = value
                if abs(value) > 0.3:
                    direction = "positive" if value > 0 else "negative"
                    new_input = ("axis", axis_idx, direction)
                    chosen_input[0] = new_input
                    selection_var.set(f"Selected: axis {axis_idx} ({direction})")
                    confirm_button.config(state=tk.NORMAL)
                
        # Check buttons
        for btn_idx in range(joystick.get_numbuttons()):
            if joystick.get_button(btn_idx):
                new_input = ("button", btn_idx)
                chosen_input[0] = new_input
                selection_var.set(f"Selected: button {btn_idx}")
                confirm_button.config(state=tk.NORMAL)
        
        # Only continue with detection if the dialog still exists
        if dialog.winfo_exists():
            dialog.after(50, update_detection)
    
    # Event handlers
    def on_confirm():
        dialog.destroy()
    
    def on_cancel():
        chosen_input[0] = None
        dialog.destroy()
    
    # Button callbacks
    confirm_button.config(command=on_confirm)
    cancel_button.config(command=on_cancel)
    
    # Start detection
    update_detection()
    
    # Wait until the dialog is closed
    dialog.wait_window(dialog)
    
    # Show what was selected in console (for consistency with CLI version)
    if chosen_input[0]:
        if chosen_input[0][0] == "button":
            print(f"Selected input: button {chosen_input[0][1]}")
        else:
            print(f"Selected input: axis {chosen_input[0][1]} ({chosen_input[0][2]})")
    
    # Process the result
    return chosen_input[0]

# --- Aanhaken aan de main-module voor verbeterde GUI-interactie ---
def setup_gui_enhancements():
    """
    Vervang bepaalde functies van main.py door GUI-verbeterde versies als de tray-app draait
    """
    if hasattr(main, 'wait_for_mapping_input'):
        # Back-up de originele functie
        main._original_wait_for_mapping_input = main.wait_for_mapping_input
        # Vervang door GUI-vriendelijke versie
        main.wait_for_mapping_input = gui_wait_for_mapping_input
        print("[DEBUG] Enhanced wait_for_mapping_input with GUI version")
    
    if hasattr(main, 'wait_for_preset_button_mapping'):
        # Back-up de originele functie
        main._original_wait_for_preset_button_mapping = main.wait_for_preset_button_mapping
        # Vervang door GUI-vriendelijke versie
        main.wait_for_preset_button_mapping = gui_wait_for_preset_button_mapping
        print("[DEBUG] Enhanced wait_for_preset_button_mapping with GUI version")

def gui_wait_for_preset_button_mapping(device):
    """
    GUI-friendly version of waiting for preset button mapping.
    """
    # Similar implementation as gui_wait_for_mapping_input but only for buttons
    dialog = tk.Toplevel(root)
    dialog.title("Map Preset Button")
    dialog.geometry("400x300")
    dialog.transient(root)
    dialog.grab_set()
    
    chosen_button = [None]
    
    # Instructions
    tk.Label(dialog, text="Press a button to assign to this preset").pack(pady=10)
    
    # Selection display
    selection_var = tk.StringVar(value="No button selected yet")
    tk.Label(dialog, textvariable=selection_var).pack(pady=20)
    
    # Button frame
    button_frame = tk.Frame(dialog)
    button_frame.pack(side=tk.BOTTOM, pady=20)
    
    # Confirm button (initially disabled)
    confirm_button = tk.Button(button_frame, text="Confirm", state=tk.DISABLED)
    confirm_button.pack(side=tk.LEFT, padx=10)
    
    # Cancel button
    cancel_button = tk.Button(button_frame, text="Cancel")
    cancel_button.pack(side=tk.LEFT, padx=10)
    
    # Update function
    def update_detection():
        pygame.event.pump()
        
        for btn_idx in range(device.get_numbuttons()):
            if device.get_button(btn_idx):
                chosen_button[0] = btn_idx
                selection_var.set(f"Selected button: {btn_idx}")
                confirm_button.config(state=tk.NORMAL)
                time.sleep(0.2)  # Basic debounce
        
        if dialog.winfo_exists():
            dialog.after(50, update_detection)
    
    # Event handlers
    def on_confirm():
        dialog.destroy()
    
    def on_cancel():
        chosen_button[0] = None
        dialog.destroy()
    
    # Button callbacks
    confirm_button.config(command=on_confirm)
    cancel_button.config(command=on_cancel)
    
    # Start detection
    update_detection()
    
    # Wait until the dialog is closed
    dialog.wait_window(dialog)
    
    # Show result in console
    if chosen_button[0] is not None:
        print(f"Selected Button: {chosen_button[0]}")
    
    return ("button", chosen_button[0]) if chosen_button[0] is not None else None


# --- Resource Laden ---
def resource_path(relative_path):
    try:
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_path, relative_path)
    except Exception:
        return relative_path

# --- GUI: Gesimuleerde Terminal ---
class APCRControllerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(TITLE)
        self.root.geometry("900x600")
        # Stel het window-icoon in als er een icon.ico is
        ico = resource_path(ICON_PATH_ICO)
        if os.path.exists(ico):
            try:
                self.root.iconbitmap(ico)
            except Exception as e:
                print("[WARNING] Kan icoon niet instellen:", e)
        # Bij sluiten verbergen we het venster in plaats van het proces te beëindigen
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        self.main_frame = ttk.Frame(root)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Console output
        self.console_frame = ttk.LabelFrame(self.main_frame, text="Console Output")
        self.console_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.console = scrolledtext.ScrolledText(
            self.console_frame, wrap=tk.WORD,
            background="black", foreground="white",
            state="disabled", font=("Consolas", 10)
        )
        self.console.pack(fill=tk.BOTH, expand=True)
        self.console.tag_config("error", foreground="red")
        self.console.tag_config("warning", foreground="yellow")
        self.console.tag_config("info", foreground="green")
        self.console.tag_config("debug", foreground="gray")
        self.console.tag_config("command", foreground="cyan")
        
        # Invoerveld voor commando's
        self.input_frame = ttk.Frame(self.main_frame)
        self.input_frame.pack(fill=tk.X, padx=5, pady=5)
        self.prompt_label = ttk.Label(self.input_frame, text="> ")
        self.prompt_label.pack(side=tk.LEFT)
        self.input_field = ttk.Entry(self.input_frame)
        self.input_field.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_field.bind("<Return>", self.send_command)
        self.send_button = ttk.Button(self.input_frame, text="Send", command=self.send_command)
        self.send_button.pack(side=tk.RIGHT)
        
        # Statusbar
        self.status_var = tk.StringVar(value="Ready")
        self.statusbar = ttk.Label(root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.statusbar.pack(side=tk.BOTTOM, fill=tk.X)
        
        # Commandgeschiedenis
        self.command_history = []
        self.history_index = -1
        self.input_field.bind("<Up>", self.previous_command)
        self.input_field.bind("<Down>", self.next_command)
        self.input_field.focus()
        
        # Variabelen voor verbindingstracking
        self.last_timeout_notification_time = 0
        self.ready_message_shown = False
        self.startup_timeout = threading.Timer(15.0, self.end_startup_phase)
        self.startup_timeout.daemon = True
        self.startup_timeout.start()
        
        self.poll_output()
    
    def end_startup_phase(self):
        """Beëindig de startupfase na een bepaalde tijd"""
        global app_startup_phase, listener_ip, connected_camids
        app_startup_phase = False
        
        # Als we het listener_ip hebben gedetecteerd, toon een ready melding (ALLEEN als er geen verbindingen zijn)
        if listener_ip and not self.ready_message_shown and not connected_camids:
            if tray_icon:
                tray_icon.notify(f"Ready to connect to APC-R on {listener_ip}", TITLE)
                print(f"[READY] APC-R Controller is ready to connect on {listener_ip}")
            self.ready_message_shown = True
    

    def poll_output(self):
        global waiting_for_enter, event_loop_terminating, menu_state, listener_ip, connected_camids
        try:
            while True:
                line = output_queue.get_nowait()
                self.append_console_text(line)
                
                # Controleer eerst voor verbindingen, vóór we de ready message overwegen
                self.check_for_connection(line)
                
                # Detecteer het IP-adres van de listener
                ip_match = re.search(r"UDP Listener initialized on (\d+\.\d+\.\d+\.\d+)", line)
                if ip_match:
                    detected_ip = ip_match.group(1)
                    if detected_ip != "0.0.0.0":  # Alleen bijwerken als het geen "alle interfaces" is
                        listener_ip = detected_ip
                        # Als de startup fase voorbij is en we nog geen ready message hebben getoond
                        # EN er zijn nog geen verbindingen
                        if not app_startup_phase and not self.ready_message_shown and not connected_camids:
                            if tray_icon:
                                tray_icon.notify(f"Ready to connect to APC-R on {listener_ip}", TITLE)
                                print(f"[READY] APC-R Controller is ready to connect on {listener_ip}")
                            self.ready_message_shown = True
                
                
                # Controleer connectie nadat IP is gedetecteerd
                self.check_for_connection(line)
                
                # Detecteren van event loop terminatie en 'Press Enter' prompts
                if "Event loop terminated. Press Enter twice to continue..." in line:
                    waiting_for_enter = True
                    event_loop_terminating = True
                    # Stuur automatisch twee keer een lege Enter om door te gaan
                    self.root.after(200, lambda: custom_stdin.write(""))
                    self.root.after(400, lambda: custom_stdin.write(""))
                
                # Detecteren van hoofdmenu prompt
                if "Type 'map', 'settings', 'presets', 'start', 'help', or 'quit'" in line:
                    menu_state = "main_menu"
                    waiting_for_enter = False
                    event_loop_terminating = False
                
                # Detecteren van settings menu
                if "Change settings for APC-R:" in line:
                    menu_state = "settings"
                    waiting_for_enter = False
                
                # Detecteren van mapping menu
                if "Detected the following input devices:" in line or "Select a mapping action:" in line:
                    menu_state = "mapping"
                    waiting_for_enter = False
                
                # Detecteren of we in de event loop zijn
                if "> " in line and menu_state != "main_menu" and menu_state != "settings" and menu_state != "mapping":
                    menu_state = "event_loop"
                
        except queue.Empty:
            pass
        self.root.after(100, self.poll_output)
    
    def append_console_text(self, text, tag=None):
        self.console.configure(state="normal")
        if tag is None:
            if "[ERROR]" in text:
                tag = "error"
            elif "[WARNING]" in text:
                tag = "warning"
            elif "[INFO]" in text:
                tag = "info"
            elif "[DEBUG]" in text:
                tag = "debug"
        self.console.insert(tk.END, text, tag)
        self.console.see(tk.END)
        self.console.configure(state="disabled")
    
    def send_command(self, event=None):
        cmd = self.input_field.get().strip()
        # Lege commando's ook doorsturen (nodig voor mapping confirmations)
        self.append_console_text("> " + cmd + "\n", "command")
        # Nu sturen we het commando naar de aangepaste stdin
        custom_stdin.write(cmd)
        self.command_history.append(cmd)
        self.history_index = -1
        self.input_field.delete(0, tk.END)
    
    def previous_command(self, event=None):
        if not self.command_history:
            return "break"
        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self.input_field.delete(0, tk.END)
            self.input_field.insert(0, self.command_history[-(self.history_index+1)])
        return "break"
    
    def next_command(self, event=None):
        if self.history_index > 0:
            self.history_index -= 1
            self.input_field.delete(0, tk.END)
            self.input_field.insert(0, self.command_history[-(self.history_index+1)])
        elif self.history_index == 0:
            self.history_index = -1
            self.input_field.delete(0, tk.END)
        return "break"
    
    def hide_window(self):
        self.root.withdraw()
        tray_icon.notify("APC-R Control remains active in systemtray.", TITLE)
    
    def show_window(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.update()
        self.root.attributes("-topmost", False)
        self.input_field.focus()
    
    def update_status(self, msg):
        self.status_var.set(msg)
    
    def check_for_connection(self, text):
        global connected_camids, disconnected_notified, app_startup_phase
        
        # Detecteer nieuwe verbindingen
        connection_match = re.search(r'\[(.*?)\] \(CamID (\d+)\) connected', text)
        if connection_match:
            apcr_name = connection_match.group(1)
            camid = int(connection_match.group(2))
            
            # Bij eerste connectie, niet meer in startup fase
            app_startup_phase = False
            
            # Controleer of deze CAMID al als verbonden is geregistreerd
            already_connected = (camid in connected_camids and 
                            connected_camids[camid].get("connected", False))
            
            # Sla op in de connected_camids dict
            if camid not in connected_camids:
                connected_camids[camid] = {}
            connected_camids[camid]["name"] = apcr_name
            connected_camids[camid]["connected"] = True
            
            # Verwijder uit de disconnected_notified set als deze er in zat
            if camid in disconnected_notified:
                disconnected_notified.remove(camid)
            
            # Toon een notificatie ALLEEN als het een nieuwe verbinding is
            if not already_connected and tray_icon:
                tray_icon.notify(f"APC-R {apcr_name} (CamID {camid}) connected", TITLE)
                print(f"[NOTIFICATION] APC-R {apcr_name} (CamID {camid}) connected")
        
        # Detecteer specifieke foutmeldingen die op verbrekingen kunnen wijzen
        # 1. No current position available
        error_match_position = re.search(r"No current position available for (.*?) \(CamID (\d+)\)", text)
        if error_match_position:
            apcr_name = error_match_position.group(1)
            camid = int(error_match_position.group(2))
            self.handle_potential_disconnection(apcr_name, camid)
        
        # 2. No new position data received
        error_match_no_data = re.search(r"No new position data received from (.*?) \(CamID (\d+)\)", text)
        if error_match_no_data:
            apcr_name = error_match_no_data.group(1)
            camid = int(error_match_no_data.group(2))
            self.handle_potential_disconnection(apcr_name, camid)
        
        # 3. Failed to send APC-R command
        error_match_send = re.search(r"Failed to send APC-R command.*?CamID (\d+) \((.*?)\)", text)
        if error_match_send:
            camid = int(error_match_send.group(1))
            apcr_name = error_match_send.group(2)
            self.handle_potential_disconnection(apcr_name, camid)
            
        # Als laatste redmiddel, detecteer algemene timeout resets
        if "consecutive timeouts. Resetting socket" in text:
            # Negeer tijdens opstartfase
            if app_startup_phase:
                return
                
            current_time = time.time()
            
            # Alleen melden als we niet te recent al een melding hebben getoond
            if not hasattr(self, 'last_timeout_notification_time') or current_time - self.last_timeout_notification_time > 30:
                self.last_timeout_notification_time = current_time
                
                # Zoek naar verbonden camera's die mogelijk disconnected zijn
                disconnected_any = False
                for camid, info in connected_camids.items():
                    if info.get("connected", False) and camid not in disconnected_notified:
                        apcr_name = info.get("name", f"CamID {camid}")
                        self.handle_potential_disconnection(apcr_name, camid)
                        disconnected_any = True
                
                # Als we geen specifieke CAMID hebben kunnen koppelen maar er waren wel verbindingen
                if not disconnected_any and connected_camids:
                    if tray_icon:
                        tray_icon.notify("Possible connection issue detected", TITLE)
                        print("[NOTIFICATION] Possible connection issue detected (timeout resets)")
    
    def handle_potential_disconnection(self, apcr_name, camid):
        global connected_camids, disconnected_notified
        
        # Alleen melden als de CAMID als verbonden was gemarkeerd en nog niet gemeld
        if camid in connected_camids and connected_camids[camid].get("connected", False) and camid not in disconnected_notified:
            # Markeer als niet-verbonden
            connected_camids[camid]["connected"] = False
            
            # Voeg toe aan de set van gemelde disconnections
            disconnected_notified.add(camid)
            
            # Toon een notificatie
            if tray_icon:
                tray_icon.notify(f"APC-R {apcr_name} (CamID {camid}) connection lost", TITLE)
                print(f"[NOTIFICATION] APC-R {apcr_name} (CamID {camid}) connection lost")


# --- Achtergrondfunctionaliteit ---
def run_main_app():
    try:
        main.main()
    except Exception as e:
        print("[ERROR] Exception in main.main():", e)

# --- Slimme menu navigatie ---
def navigate_to_menu(target_menu, stop_event_loop=True):
    """
    Navigeer naar het gewenste menu door de juiste commando's in de juiste volgorde te sturen
    
    Args:
        target_menu: Het doelmenu ('help', 'settings', 'map')
        stop_event_loop: Of de event_loop gestopt moet worden (niet nodig voor help)
    """
    global menu_state, waiting_for_enter, event_loop_terminating
    
    # First show the window
    if app:
        app.show_window()
    
    # Als we niet naar help gaan en we zijn in de event loop, stuur 'quit' om de loop te stoppen
    if target_menu != "help" and stop_event_loop:
        # Controleer of we in start mode zijn (event loop actief)
        if menu_state == "idle" or menu_state == "event_loop":
            print(f"[DEBUG] Stuur 'quit' om event_loop te stoppen voor {target_menu}")
            custom_stdin.write("quit")
            # Nu moeten we wachten tot we in het hoofdmenu terugkeren
    
    # Een timer om te controleren wanneer we in de juiste staat zijn
    def check_state_and_continue():
        global menu_state, waiting_for_enter, event_loop_terminating
        
        # Als we in een event loop termination zijn, moeten we wachten
        if event_loop_terminating or waiting_for_enter:
            print("[DEBUG] Wachten tot terminatie afgerond is...")
            # Stuur automatisch enter om door te gaan
            custom_stdin.write("")
            # Controleer opnieuw over 500ms
            app.root.after(500, check_state_and_continue)
            return
            
        # Voor help-menu hoeven we de event loop niet af te sluiten
        if target_menu == "help":
            # Als we al in de event loop zijn, stuur direct help
            if menu_state == "event_loop":
                custom_stdin.write("help")
            # Als we in het hoofdmenu zijn, stuur start en dan help
            elif menu_state == "main_menu":
                custom_stdin.write("start")
                app.root.after(500, lambda: custom_stdin.write("help"))
            # Als we in settings/mapping zijn, ga terug naar hoofdmenu
            elif menu_state in ["settings", "mapping"]:
                custom_stdin.write("return")
                app.root.after(500, lambda: custom_stdin.write("start"))
                app.root.after(1000, lambda: custom_stdin.write("help"))
            else:
                custom_stdin.write("help")
            return
                
        # Voor settings/mapping: als we in het hoofdmenu zijn, open het gewenste menu
        if menu_state == "main_menu":
            print(f"[DEBUG] In hoofdmenu, nu naar {target_menu} gaan")
            if target_menu == "settings":
                custom_stdin.write("settings")
            elif target_menu == "map":
                custom_stdin.write("map")
        # Als we al in het gewenste menu zijn, doen we niks
        elif menu_state == target_menu:
            print(f"[DEBUG] Al in {target_menu} menu, niks te doen")
        # In alle andere gevallen, proberen we terug te gaan naar het hoofdmenu
        else:
            print(f"[DEBUG] In {menu_state}, terug naar hoofdmenu proberen")
            # In settings of mapping kunnen we 'return' gebruiken
            if menu_state in ["settings", "mapping"]:
                custom_stdin.write("return")
                # Controleer opnieuw na een korte pauze
                app.root.after(1000, check_state_and_continue)
            # In andere staten kunnen we proberen om naar het hoofdmenu terug te gaan
            else:
                custom_stdin.write("quit")
                # Controleer opnieuw na een korte pauze
                app.root.after(1000, check_state_and_continue)
    
    # Start het proces
    app.root.after(100, check_state_and_continue)

# --- Tray Icon Handlers ---
def on_tray_show(icon, item):
    # Show APC-R Control moet de event loop niet stoppen
    navigate_to_menu("help", stop_event_loop=False)

def on_tray_settings(icon, item):
    # Settings moet wel de event loop stoppen
    navigate_to_menu("settings", stop_event_loop=True)

def on_tray_mapping(icon, item):
    # Mapping moet wel de event loop stoppen
    navigate_to_menu("map", stop_event_loop=True)

def on_tray_restart(icon, item):
    print("[INFO] Herstart APC-R Controller...")
    icon.stop()
    time.sleep(0.5)
    os.execv(sys.executable, [sys.executable] + sys.argv)

def on_tray_exit(icon, item):
    print("[INFO] Sluiten APC-R Controller...")
    icon.stop()
    if root:
        root.quit()
    os._exit(0)

def create_tray_icon():
    menu = pystray.Menu(
        pystray.MenuItem("Show APC-R Control", on_tray_show),
        pystray.MenuItem("Settings", on_tray_settings),
        pystray.MenuItem("Mappings", on_tray_mapping),
        pystray.MenuItem("Restart", on_tray_restart),
        pystray.MenuItem("Exit", on_tray_exit)
    )
    try:
        ico_path = resource_path(ICON_PATH_ICO)
        if os.path.exists(ico_path):
            image = Image.open(ico_path)
        elif os.path.exists(resource_path(ICON_PATH_PNG)):
            image = Image.open(resource_path(ICON_PATH_PNG))
        else:
            # Fallback-icoon
            image = Image.new('RGBA', (64, 64), color=(73, 109, 137, 255))
            draw = ImageDraw.Draw(image)
            draw.ellipse((10, 10, 54, 54), fill=(255, 255, 255, 128))
    except Exception as e:
        print("[WARNING] Error loading tray icon:", e)
        image = Image.new('RGBA', (64, 64), color=(73, 109, 137, 255))
    return pystray.Icon(TITLE, image, TITLE, menu)

def start_tray():
    global tray_icon
    tray_icon = create_tray_icon()
    tray_icon.run()

# --- Main entry point ---
def main_app():
    global root, app, tray_icon
    # Start de hoofdfunctionaliteit in een daemon-thread
    main_thread = threading.Thread(target=run_main_app, daemon=True)
    main_thread.start()
    
    # Maak de Tkinter-GUI (initialiseer maar verberg deze direct)
    root = tk.Tk()
    app = APCRControllerApp(root)
    
    # Verbeter de main.py functies met GUI-versies
    setup_gui_enhancements()


    # Start verborgen in de tray
    root.withdraw()
     
    # Start de tray icon in een aparte daemon-thread
    tray_thread = threading.Thread(target=start_tray, daemon=True)
    tray_thread.start()
    
    # Start de Tkinter mainloop
    root.mainloop()

if __name__ == "__main__":
    main_app()
