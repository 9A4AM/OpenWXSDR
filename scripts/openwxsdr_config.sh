#!/bin/bash
# OpenWXSDR Configuration Script
# Interactive configuration tool for config.yaml using whiptail (raspi-config style)
#
# Design notes:
#  - The whole config.yaml is read ONCE at startup into an in-memory cache
#    (single python invocation) so menus open instantly. The cache is updated
#    in place whenever a value is written, so values are only re-read when they
#    actually change.
#  - Values are edited in place: comments and formatting are preserved.
#  - Cancel/Esc in any setting or submenu returns to the main menu.

# NOTE: 'set -e' is intentionally NOT used. whiptail returns non-zero on
# Cancel/Esc which is normal control flow here.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="$PROJECT_ROOT/config.yaml"
BACKUP_DIR="$PROJECT_ROOT/data/config_backups"

# Application version (single source of truth: src/__init__.py)
APP_VERSION="$(sed -nE "s/^__version__[[:space:]]*=[[:space:]]*['\"]([^'\"]+)['\"].*/\1/p" "$PROJECT_ROOT/src/__init__.py" 2>/dev/null | head -n1)"
[ -z "$APP_VERSION" ] && APP_VERSION="unknown"

# Width used for centring text inside the whiptail box
CENTER_WIDTH=72

# Center a single text line within CENTER_WIDTH (no trailing newline).
center_line() {
    local text="$1" len pad
    len=${#text}
    pad=$(( (CENTER_WIDTH - len) / 2 ))
    (( pad < 0 )) && pad=0
    printf '%*s%s' "$pad" '' "$text"
}

# Center a multi-line block as a whole (every line shifted by the same amount,
# based on the widest line, so the ASCII art stays aligned). Reads from stdin.
center_block() {
    local line maxlen=0 pad first=1
    local -a lines=()
    while IFS= read -r line; do
        lines+=("$line")
        (( ${#line} > maxlen )) && maxlen=${#line}
    done
    pad=$(( (CENTER_WIDTH - maxlen) / 2 ))
    (( pad < 0 )) && pad=0
    for line in "${lines[@]}"; do
        (( first )) || printf '\n'
        first=0
        printf '%*s%s' "$pad" '' "$line"
    done
}

# ASCII logo parts (plain text; whiptail renders no colours)
ASCII_ART="$(cat <<'LOGOEOF'
   ___                __        ____  ______  ____  ____
  / _ \ _ __   ___ _ _\ \      / /\ \/ / ___||  _ \|  _ \
 | | | |  _ \ / _ \ |  \ \ /\ / /  \  /\___ \| | | | |_) |
 | |_| | |_) |  __/ | | \ V  V /   /  \ ___) | |_| |  _ <
  \___/| .__/ \___|_| |_|\_/\_/   /_/\_\____/|____/|_| \_\
       |_|
LOGOEOF
)"
COPYRIGHT_LINE='Copyright (C) 2026 Meinhard F. Guenther, DL2MF@darc.de'
SLOGAN_LINE='Lightweight radiosonde gateway for Raspberry Pi'

# Pre-built centred banner: ASCII art + copyright + slogan
LOGO_BANNER="$(printf '%s\n' "$ASCII_ART" | center_block)
$(center_line "$COPYRIGHT_LINE")

$(center_line "$SLOGAN_LINE")"

WT_HEIGHT=22
WT_WIDTH=78
WT_MENU_HEIGHT=12

# Global flag: when set to 1, all nested menus unwind back to the main menu
RETURN_TO_MAIN=0

# In-memory value cache (path -> value)
declare -A CFG

#==============================================================================
# Embedded Python YAML editor (stdlib only, preserves comments/formatting)
#==============================================================================

PYTOOL="$(mktemp "${TMPDIR:-/tmp}/openwxsdr_yaml.XXXXXX.py")"
trap 'rm -f "$PYTOOL"' EXIT

cat > "$PYTOOL" <<'PYEOF'
import sys, re

def eff_indent(line):
    raw = len(line) - len(line.lstrip(' '))
    rest = line[raw:]
    return raw + 2 if rest.startswith('- ') else raw

def is_content(line):
    s = line.strip()
    return bool(s) and not s.startswith('#')

def parse_tokens(p):
    toks = []
    for part in p.split('.'):
        m = re.match(r'^([^\[]+)(?:\[(\d+)\])?$', part)
        toks.append((m.group(1), int(m.group(2)) if m.group(2) is not None else None))
    return toks

def region_indent_of(lines, start, end):
    for i in range(start, end):
        if is_content(lines[i]):
            return eff_indent(lines[i])
    return None

def block_end(lines, start, end, region_indent):
    e = start
    while e < end:
        if is_content(lines[e]) and eff_indent(lines[e]) <= region_indent:
            break
        e += 1
    return e

def find_leaf(lines, start, end, tokens):
    if not tokens:
        return None
    key, idx = tokens[0]
    ri = region_indent_of(lines, start, end)
    if ri is None:
        return None
    keyre = re.compile(r'^\s*(?:- )?' + re.escape(key) + r'\s*:')
    i = start
    while i < end:
        line = lines[i]
        if is_content(line) and eff_indent(line) == ri and keyre.match(line):
            cend = block_end(lines, i + 1, end, ri)
            if idx is None:
                if len(tokens) == 1:
                    return i
                res = find_leaf(lines, i + 1, cend, tokens[1:])
                if res is not None:
                    return res
            else:
                items = []
                dash_indent = None
                j = i + 1
                while j < cend:
                    lj = lines[j]
                    if is_content(lj):
                        raw = len(lj) - len(lj.lstrip(' '))
                        if lj[raw:].startswith('- '):
                            if dash_indent is None:
                                dash_indent = raw
                            if raw == dash_indent:
                                items.append(j)
                    j += 1
                if idx < len(items):
                    istart = items[idx]
                    iend = items[idx + 1] if idx + 1 < len(items) else cend
                    if len(tokens) == 1:
                        return istart
                    res = find_leaf(lines, istart, iend, tokens[1:])
                    if res is not None:
                        return res
        i += 1
    return None

def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
        return v[1:-1]
    return v

def clean_value(s):
    in_s = in_d = False
    ci = -1
    for k, ch in enumerate(s):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == '#' and not in_s and not in_d and (k == 0 or s[k - 1] in ' \t'):
            ci = k
            break
    if ci >= 0:
        s = s[:ci]
    return unquote(s.strip())

def split_value(line):
    nl = '\n' if line.endswith('\n') else ''
    core = line[:-1] if nl else line
    m = re.match(r'^(\s*(?:- )?[^:]+:)(\s*)(.*)$', core)
    if not m:
        return None
    prefix, sep, rest = m.group(1), m.group(2), m.group(3)
    in_s = in_d = False
    ci = -1
    for k, ch in enumerate(rest):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == '#' and not in_s and not in_d and (k == 0 or rest[k - 1] in ' \t'):
            ci = k
            break
    if ci >= 0:
        val = rest[:ci].rstrip()
        comment = rest[ci:]
    else:
        val = rest.rstrip()
        comment = ''
    return prefix, sep, val, comment, nl

def fmt(newval, oldval):
    ov = oldval.strip()
    if ov[:1] in ("'", '"'):
        q = ov[0]
        return q + newval + q
    if newval == '' or newval in ('true', 'false', 'null') \
       or re.match(r'^-?\d+(\.\d+)?$', newval) or newval.startswith('['):
        return newval
    if re.search(r'[\s:#]', newval):
        return "'" + newval + "'"
    return newval

def enumerate_leaves(lines):
    out = []
    stack = [{'indent': -1, 'path': '', 'is_seq': False, 'idx': -1}]
    for line in lines:
        if not is_content(line):
            continue
        raw = len(line) - len(line.lstrip(' '))
        rest = line[raw:]
        dash = rest.startswith('- ')
        ci = raw + (2 if dash else 0)
        while len(stack) > 1 and stack[-1]['indent'] is not None and stack[-1]['indent'] > ci:
            stack.pop()
        if stack[-1]['indent'] is None:
            stack[-1]['indent'] = ci
            stack[-1]['is_seq'] = dash
        if dash:
            while len(stack) > 1 and stack[-1]['indent'] == ci and not stack[-1]['is_seq']:
                stack.pop()
            if stack[-1]['indent'] is None:
                stack[-1]['indent'] = ci
                stack[-1]['is_seq'] = True
            parent = stack[-1]
            parent['is_seq'] = True
            parent['idx'] += 1
            item_prefix = parent['path'] + '[' + str(parent['idx']) + ']'
            inner = rest[2:]
            m = re.match(r'^([^:\s][^:]*):(.*)$', inner)
            if m:
                key = m.group(1).strip()
                valpart = m.group(2)
                full = item_prefix + '.' + key
                if valpart.strip() == '':
                    stack.append({'indent': None, 'path': full, 'is_seq': False, 'idx': -1})
                else:
                    out.append((full, clean_value(valpart)))
                    stack.append({'indent': None, 'path': item_prefix, 'is_seq': False, 'idx': -1})
            else:
                out.append((item_prefix, clean_value(inner)))
        else:
            m = re.match(r'^([^:\s][^:]*):(.*)$', rest)
            if not m:
                continue
            key = m.group(1).strip()
            valpart = m.group(2)
            parent = stack[-1]
            full = (parent['path'] + '.' + key) if parent['path'] else key
            if valpart.strip() == '':
                stack.append({'indent': None, 'path': full, 'is_seq': False, 'idx': -1})
            else:
                out.append((full, clean_value(valpart)))
    return out

def grab_block(lines, leaf):
    li = len(lines[leaf]) - len(lines[leaf].lstrip(' '))
    block = [lines[leaf]]
    j = leaf - 1
    while j >= 0:
        s = lines[j]
        if s.strip() == '':
            break
        stripped = s.lstrip(' ')
        raw = len(s) - len(stripped)
        if stripped.startswith('#') and raw == li:
            block.insert(0, s)
            j -= 1
        else:
            break
    return block

def reindent(block, from_indent, to_indent):
    delta = to_indent - from_indent
    out = []
    for ln in block:
        if delta > 0:
            out.append(' ' * delta + ln)
        elif delta < 0:
            rem = -delta
            i = 0
            while i < rem and i < len(ln) and ln[i] == ' ':
                i += 1
            out.append(ln[i:])
        else:
            out.append(ln)
    return out

def _last_child(lines, parent_idx, ri):
    last = parent_idx
    e = parent_idx + 1
    n = len(lines)
    while e < n:
        if is_content(lines[e]):
            if eff_indent(lines[e]) <= ri:
                break
            last = e
        e += 1
    return last

def do_fixkey(ex, tg, key):
    tokens = parse_tokens(key)
    leaf = find_leaf(ex, 0, len(ex), tokens)
    if leaf is None:
        return None
    block = grab_block(ex, leaf)
    leaf_indent = eff_indent(ex[leaf])
    parent = tokens[:-1]
    if not parent:
        newlines = reindent(block, leaf_indent, 0)
        if tg and tg[-1].strip() != '':
            newlines = ['\n'] + newlines
        return tg + newlines
    pleaf = find_leaf(tg, 0, len(tg), parent)
    if pleaf is not None:
        ri = eff_indent(tg[pleaf])
        insert_pos = _last_child(tg, pleaf, ri) + 1
        tg[insert_pos:insert_pos] = reindent(block, leaf_indent, ri + 2)
        return tg
    depth = len(parent)
    existing = None
    while depth > 0:
        existing = find_leaf(tg, 0, len(tg), parent[:depth])
        if existing is not None:
            break
        depth -= 1
    if existing is not None:
        ri = eff_indent(tg[existing])
        base_indent = ri + 2
        insert_pos = _last_child(tg, existing, ri) + 1
        lead = []
    else:
        base_indent = 0
        insert_pos = len(tg)
        lead = ['\n'] if (tg and tg[-1].strip() != '') else []
    newlines = []
    ind = base_indent
    for (k, _idx) in parent[depth:]:
        newlines.append(' ' * ind + k + ':\n')
        ind += 2
    newlines += reindent(block, leaf_indent, ind)
    tg[insert_pos:insert_pos] = lead + newlines
    return tg

def main():
    action = sys.argv[1]
    if action == 'fixkey':
        ex_path, tg_path, key = sys.argv[2], sys.argv[3], sys.argv[4]
        with open(ex_path) as f:
            ex = f.readlines()
        with open(tg_path) as f:
            tg = f.readlines()
        res = do_fixkey(ex, tg, key)
        if res is None:
            sys.exit(5)
        with open(tg_path, 'w') as f:
            f.writelines(res)
        return
    path = sys.argv[2]
    with open(path) as f:
        lines = f.readlines()
    if action == 'getall':
        for k, v in enumerate_leaves(lines):
            sys.stdout.write(k + '\t' + v + '\n')
        return
    key = sys.argv[3]
    leaf = find_leaf(lines, 0, len(lines), parse_tokens(key))
    if action == 'get':
        if leaf is None:
            print('')
            return
        parts = split_value(lines[leaf])
        print(clean_value(parts[2]) if parts else '')
    elif action == 'set':
        newval = sys.argv[4] if len(sys.argv) > 4 else ''
        if leaf is None:
            sys.exit(2)
        parts = split_value(lines[leaf])
        if not parts:
            sys.exit(3)
        prefix, sep, val, comment, nl = parts
        sep = sep if sep else ' '
        line = prefix + sep + fmt(newval, val)
        if comment:
            line += ' ' + comment
        line += nl if nl else '\n'
        lines[leaf] = line
        with open(path, 'w') as f:
            f.writelines(lines)

main()
PYEOF

#==============================================================================
# Cache + YAML helpers
#==============================================================================

# Load the entire config into the CFG cache in a single python call.
load_cache() {
    CFG=()
    local k v
    while IFS=$'\t' read -r k v; do
        CFG["$k"]="$v"
    done < <(python3 "$PYTOOL" getall "$CONFIG_FILE" 2>/dev/null)
}

# Read a value from the cache (instant). Falls back to a live read if missing.
yaml_read() {
    local k="$1"
    if [ "${CFG[$k]+x}" = "x" ]; then
        printf '%s' "${CFG[$k]}"
    else
        local v
        v=$(python3 "$PYTOOL" get "$CONFIG_FILE" "$k" 2>/dev/null)
        CFG["$k"]="$v"
        printf '%s' "$v"
    fi
}

# Write a value and update the cache in place (no full reload needed).
yaml_write() {
    if ! python3 "$PYTOOL" set "$CONFIG_FILE" "$1" "$2" 2>/dev/null; then
        show_error "Could not update '$1' — key not found in config.yaml.\nEdit it manually if needed."
        return 1
    fi
    CFG["$1"]="$2"
    return 0
}

# Menu item formatter: label left-padded to 26, then >= 8 spaces, then [value].
mi() {
    printf '%-26s        [%s]' "$1" "$2"
}

#==============================================================================
# Utility Functions
#==============================================================================

check_dependencies() {
    if ! command -v whiptail &> /dev/null; then
        echo -e "${RED}Error: whiptail is not installed${NC}"
        echo "Install with: sudo apt-get install whiptail"
        exit 1
    fi
    if ! command -v python3 &> /dev/null; then
        echo -e "${RED}Error: python3 is not installed${NC}"
        echo "python3 is required for YAML editing."
        exit 1
    fi
}

backup_config() {
    mkdir -p "$BACKUP_DIR"
    local backup_file="$BACKUP_DIR/config.yaml.$(date +%Y%m%d_%H%M%S)"
    cp "$CONFIG_FILE" "$backup_file"
    echo -e "${GREEN}Backup created: $backup_file${NC}"
}

show_message() {
    whiptail --title "$1" --msgbox "$2" $WT_HEIGHT $WT_WIDTH
}

show_error() {
    whiptail --title "Error" --msgbox "$1" $WT_HEIGHT $WT_WIDTH
}

# Generic: edit a text value. Cancel/Esc -> return to main menu.
edit_text() {
    local key="$1" title="$2" prompt="$3"
    local cur new
    cur=$(yaml_read "$key")
    new=$(whiptail --title "$title" --cancel-button "Main Menu" \
        --inputbox "$prompt" $WT_HEIGHT $WT_WIDTH "$cur" 3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; return 1; }
    yaml_write "$key" "$new" && show_message "$title" "Set to: $new"
    return 0
}

# Generic: edit a numeric value with optional [min] [max]. Cancel/Esc -> main.
edit_num() {
    local key="$1" title="$2" prompt="$3" min="$4" max="$5"
    local cur new
    cur=$(yaml_read "$key")
    new=$(whiptail --title "$title" --cancel-button "Main Menu" \
        --inputbox "$prompt" $WT_HEIGHT $WT_WIDTH "$cur" 3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; return 1; }
    if ! echo "$new" | grep -Eq '^-?[0-9]+([.][0-9]+)?$'; then
        show_error "Invalid number: '$new'"
        return 0
    fi
    if [ -n "$min" ] && awk "BEGIN{exit !($new < $min)}"; then
        show_error "Value must be >= $min"
        return 0
    fi
    if [ -n "$max" ] && awk "BEGIN{exit !($new > $max)}"; then
        show_error "Value must be <= $max"
        return 0
    fi
    yaml_write "$key" "$new" && show_message "$title" "Set to: $new"
    return 0
}

# Generic: toggle a boolean. Yes=true, No=false, Esc=return to main.
edit_bool() {
    local key="$1" title="$2" prompt="$3"
    local cur rc
    cur=$(yaml_read "$key")
    whiptail --title "$title" --yesno "$prompt\n\nCurrent value: $cur\n\n<Yes> = enable   <No> = disable   (Esc = main menu)" $WT_HEIGHT $WT_WIDTH
    rc=$?
    if [ $rc -eq 0 ]; then
        yaml_write "$key" "true" && show_message "$title" "Enabled"
    elif [ $rc -eq 1 ]; then
        yaml_write "$key" "false" && show_message "$title" "Disabled"
    else
        RETURN_TO_MAIN=1
        return 1
    fi
    return 0
}

# Generic: choose from options. Args: key title prompt tag1 desc1 tag2 desc2 ...
edit_choice() {
    local key="$1" title="$2" prompt="$3"
    shift 3
    local cur new tag desc
    cur=$(yaml_read "$key")
    local args=()
    while [ $# -ge 2 ]; do
        tag="$1"; desc="$2"; shift 2
        if [ "$tag" = "$cur" ]; then
            args+=("$tag" "$desc" "ON")
        else
            args+=("$tag" "$desc" "OFF")
        fi
    done
    new=$(whiptail --title "$title" --cancel-button "Main Menu" \
        --radiolist "$prompt\n\nCurrent: $cur" $WT_HEIGHT $WT_WIDTH 8 "${args[@]}" 3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; return 1; }
    if [ -n "$new" ]; then
        yaml_write "$key" "$new" && show_message "$title" "Set to: $new"
    fi
    return 0
}

# Return-to-main check used after each edit/submenu call inside a menu loop.
chk() {
    [ "$RETURN_TO_MAIN" != "1" ]
}

#==============================================================================
# Station Configuration
#==============================================================================

configure_station() {
    while true; do
        CHOICE=$(whiptail --title "Station Configuration" --cancel-button "Main Menu" \
            --menu "Configure station identity and location\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Callsign' "$(yaml_read station.callsign)")" \
            "2" "$(mi 'Position' "$(yaml_read station.lat), $(yaml_read station.lon), $(yaml_read station.alt)m")" \
            "3" "$(mi 'APRS Passcode' "$(yaml_read station.aprs_passcode)")" \
            "4" "$(mi 'Upload Position' "$(yaml_read station.upload_position)")" \
            "5" "$(mi 'Gateway Symbol' "$(yaml_read station.gateway_symbol)")" \
            "6" "$(mi 'Receiver & Antenna' "$(yaml_read station.receiver) / $(yaml_read station.antenna)")" \
            "7" "$(mi 'Upload Interval' "$(yaml_read station.upload_interval) min")" \
            "8" "View Current Settings" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_text station.callsign "Callsign" "Enter station callsign (e.g. DL2MF-12):"; chk || break ;;
            2)
                edit_text station.lat "Latitude" "Enter latitude (decimal degrees, N positive):"; chk || break
                edit_text station.lon "Longitude" "Enter longitude (decimal degrees, E positive):"; chk || break
                edit_num  station.alt "Altitude" "Enter altitude (metres ASL):"; chk || break
                ;;
            3) edit_text station.aprs_passcode "APRS Passcode" "Enter APRS passcode\n(Generate at https://apps.magicbug.co.uk/passcode/):"; chk || break ;;
            4) edit_bool station.upload_position "Upload Position" "Upload your listener position to networks?"; chk || break ;;
            5) edit_text station.gateway_symbol "Gateway Symbol" "Enter APRS symbol (e.g. /r = receiver, R0 = gateway):"; chk || break ;;
            6)
                edit_text station.receiver "Receiver" "Enter receiver hardware description:"; chk || break
                edit_text station.antenna "Antenna" "Enter antenna type:"; chk || break
                ;;
            7) edit_num station.upload_interval "Upload Interval" "Enter gateway status upload interval (minutes, 15-720):" 15 720; chk || break ;;
            8)
                show_message "Current Station Settings" \
"Callsign:        $(yaml_read station.callsign)
Latitude:        $(yaml_read station.lat)
Longitude:       $(yaml_read station.lon)
Altitude:        $(yaml_read station.alt) m
Upload Position: $(yaml_read station.upload_position)
Gateway Symbol:  $(yaml_read station.gateway_symbol)
Receiver:        $(yaml_read station.receiver)
Antenna:         $(yaml_read station.antenna)
Upload Interval: $(yaml_read station.upload_interval) min"
                ;;
            B) break ;;
        esac
    done
}

#==============================================================================
# SDR Configuration
#==============================================================================

configure_sdr() {
    while true; do
        CHOICE=$(whiptail --title "SDR Configuration" --cancel-button "Main Menu" \
            --menu "Configure SDR type and receiver settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Select SDR Type' "$(yaml_read sdr.type)")" \
            "2" "Configure RTL-SDR" \
            "3" "Configure KA9Q Radio" \
            "4" "Configure Flux242" \
            "5" "Configure Airspy" \
            "6" "$(mi 'Airspy Support' "$(yaml_read sdr.airspy_support)")" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_choice sdr.type "Select SDR Type" "Choose SDR type:" \
                    "rtlsdr" "RTL-SDR USB Dongle" \
                    "ka9q" "KA9Q Radio" \
                    "flux242" "Flux242 Receiver" \
                    "airspy" "Airspy Mini/R2"; chk || break ;;
            2) configure_rtlsdr; chk || break ;;
            3) configure_ka9q; chk || break ;;
            4) configure_flux242; chk || break ;;
            5) configure_airspy; chk || break ;;
            6) edit_bool sdr.airspy_support "Airspy Support" "Enable Airspy support?\n(Requires installation via install.sh)"; chk || break ;;
            B) break ;;
        esac
    done
}

configure_rtlsdr() {
    while true; do
        CHOICE=$(whiptail --title "RTL-SDR Configuration" --cancel-button "Main Menu" \
            --menu "Configure RTL-SDR devices (up to 4)\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Device 1 (Scanner)' "$(yaml_read 'sdr.rtlsdr.devices[0].serial')")" \
            "2" "$(mi 'Device 2' "$(yaml_read 'sdr.rtlsdr.devices[1].serial')")" \
            "3" "$(mi 'Device 3' "$(yaml_read 'sdr.rtlsdr.devices[2].serial')")" \
            "4" "$(mi 'Device 4' "$(yaml_read 'sdr.rtlsdr.devices[3].serial')")" \
            "B" "Back to SDR Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1|2|3|4) configure_rtlsdr_device $((CHOICE - 1)); chk || break ;;
            B) break ;;
        esac
    done
}

configure_rtlsdr_device() {
    local idx=$1
    local p="sdr.rtlsdr.devices[$idx]"
    while true; do
        CHOICE=$(whiptail --title "RTL-SDR Device $((idx + 1))" --cancel-button "Main Menu" \
            --menu "Configure device settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Serial Number' "$(yaml_read "$p.serial")")" \
            "2" "$(mi 'Center Frequency' "$(yaml_read "$p.center_freq")")" \
            "3" "$(mi 'Sample Rate' "$(yaml_read "$p.sample_rate")")" \
            "4" "$(mi 'Gain' "$(yaml_read "$p.gain")")" \
            "5" "$(mi 'PPM Error' "$(yaml_read "$p.ppm_error")")" \
            "6" "$(mi 'Decoder Mode' "$(yaml_read "$p.decoder_mode")")" \
            "7" "$(mi 'Max Channels' "$(yaml_read "$p.max_channels")")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_text "$p.serial" "Serial Number" "Enter serial number (e.g. NESDR001):"; chk || break ;;
            2) edit_num  "$p.center_freq" "Center Frequency" "Enter center frequency in Hz (e.g. 404600000):"; chk || break ;;
            3) edit_num  "$p.sample_rate" "Sample Rate" "Enter sample rate in Hz (e.g. 2400000):"; chk || break ;;
            4) edit_num  "$p.gain" "Gain" "Enter gain (0 = auto, or 0-50):" 0 50; chk || break ;;
            5) edit_num  "$p.ppm_error" "PPM Error" "Enter PPM error correction:"; chk || break ;;
            6) edit_choice "$p.decoder_mode" "Decoder Mode" "Choose decoder mode:" \
                    "legacy" "rtl_fm + rs1729 (stable)" \
                    "channelizer" "iq_dec multi-channel (experimental)"; chk || break ;;
            7) edit_num  "$p.max_channels" "Max Channels" "Enter max channels (1-4 for RTL-SDR):" 1 4; chk || break ;;
            B) break ;;
        esac
    done
}

configure_ka9q() {
    while true; do
        CHOICE=$(whiptail --title "KA9Q Radio Configuration" --cancel-button "Main Menu" \
            --menu "Configure KA9Q Radio settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Multicast Group' "$(yaml_read sdr.ka9q.multicast_group)")" \
            "2" "$(mi 'Port' "$(yaml_read sdr.ka9q.port)")" \
            "3" "$(mi 'Network Interface' "$(yaml_read sdr.ka9q.interface)")" \
            "4" "$(mi 'Dynamic Channels' "$(yaml_read sdr.ka9q.enable_dynamic_channels)")" \
            "5" "Hostnames" \
            "6" "$(mi 'Max Channels' "$(yaml_read sdr.ka9q.max_channels)")" \
            "7" "Scanning Mode" \
            "8" "$(mi 'RTP Byte Swap' "$(yaml_read sdr.ka9q.rtp_byte_swap)")" \
            "9" "View Current Settings" \
            "B" "Back to SDR Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_text sdr.ka9q.multicast_group "Multicast Group" "Enter RTP multicast group address:"; chk || break ;;
            2) edit_num  sdr.ka9q.port "Port" "Enter RTP multicast port:"; chk || break ;;
            3) edit_text sdr.ka9q.interface "Network Interface" "Enter network interface (e.g. eth0, wlan0, 0.0.0.0):"; chk || break ;;
            4) edit_bool sdr.ka9q.enable_dynamic_channels "Dynamic Channels" "Enable dynamic channel management?"; chk || break ;;
            5)
                edit_text sdr.ka9q.radio_hostname "Radio Hostname" "Enter KA9Q radio hostname:"; chk || break
                edit_text sdr.ka9q.pcm_hostname "PCM Hostname" "Enter PCM multicast hostname:"; chk || break
                ;;
            6) edit_num  sdr.ka9q.max_channels "Max Channels" "Enter max concurrent channels (1-10):" 1 10; chk || break ;;
            7) configure_ka9q_scanning; chk || break ;;
            8) edit_bool sdr.ka9q.rtp_byte_swap "RTP Byte Swap" "Enable RTP byte swapping?\n(Required on little-endian hosts)"; chk || break ;;
            9)
                show_message "KA9Q Settings" \
"Multicast:        $(yaml_read sdr.ka9q.multicast_group):$(yaml_read sdr.ka9q.port)
Interface:        $(yaml_read sdr.ka9q.interface)
Dynamic Channels: $(yaml_read sdr.ka9q.enable_dynamic_channels)
Radio Hostname:   $(yaml_read sdr.ka9q.radio_hostname)
PCM Hostname:     $(yaml_read sdr.ka9q.pcm_hostname)
Max Channels:     $(yaml_read sdr.ka9q.max_channels)
Scanning Mode:    $(yaml_read sdr.ka9q.scanning_mode)
RTP Byte Swap:    $(yaml_read sdr.ka9q.rtp_byte_swap)"
                ;;
            B) break ;;
        esac
    done
}

configure_ka9q_scanning() {
    while true; do
        CHOICE=$(whiptail --title "KA9Q Scanning Mode" --cancel-button "Main Menu" \
            --menu "Configure DFT spectrum scanning\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Scanning Mode' "$(yaml_read sdr.ka9q.scanning_mode)")" \
            "2" "$(mi 'Scan Interval' "$(yaml_read sdr.ka9q.scan_interval)")" \
            "3" "Frequency Range" \
            "4" "$(mi 'Detection Threshold' "$(yaml_read sdr.ka9q.detection_threshold)")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool sdr.ka9q.scanning_mode "Scanning Mode" "Enable DFT spectrum scanning?"; chk || break ;;
            2) edit_num  sdr.ka9q.scan_interval "Scan Interval" "Enter scan interval (seconds):"; chk || break ;;
            3)
                edit_num sdr.ka9q.scan_frequency_min "Min Frequency" "Enter minimum scan frequency (Hz):"; chk || break
                edit_num sdr.ka9q.scan_frequency_max "Max Frequency" "Enter maximum scan frequency (Hz):"; chk || break
                ;;
            4) edit_num  sdr.ka9q.detection_threshold "Detection Threshold" "Enter DFT detection threshold (dB):"; chk || break ;;
            B) break ;;
        esac
    done
}

configure_flux242() {
    while true; do
        CHOICE=$(whiptail --title "Flux242 Configuration" --cancel-button "Main Menu" \
            --menu "Configure Flux242 receiver settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Center Frequency' "$(yaml_read sdr.flux242.center_freq)")" \
            "2" "$(mi 'Sample Rate' "$(yaml_read sdr.flux242.sample_rate)")" \
            "3" "$(mi 'Gain' "$(yaml_read sdr.flux242.gain)")" \
            "4" "$(mi 'PPM Error' "$(yaml_read sdr.flux242.ppm_error)")" \
            "5" "$(mi 'Detection Threshold' "$(yaml_read sdr.flux242.threshold)")" \
            "6" "UDP Ports" \
            "7" "$(mi 'Script Path' "$(yaml_read sdr.flux242.script_path)")" \
            "8" "View Current Settings" \
            "B" "Back to SDR Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_num  sdr.flux242.center_freq "Center Frequency" "Enter center frequency in Hz:"; chk || break ;;
            2) edit_num  sdr.flux242.sample_rate "Sample Rate" "Enter sample rate in Hz (2400000 recommended):"; chk || break ;;
            3) edit_num  sdr.flux242.gain "Gain" "Enter gain (0 = auto, or 0-50):" 0 50; chk || break ;;
            4) edit_num  sdr.flux242.ppm_error "PPM Error" "Enter PPM error correction:"; chk || break ;;
            5) edit_num  sdr.flux242.threshold "Detection Threshold" "Enter detection threshold in dB (4-5 recommended):"; chk || break ;;
            6)
                edit_num sdr.flux242.udp_port "UDP Port" "Enter UDP port for decoded JSON frames:"; chk || break
                edit_num sdr.flux242.power_port "Power Port" "Enter UDP port for power scanning data:"; chk || break
                edit_num sdr.flux242.debug_port "Debug Port" "Enter UDP port for debug info:"; chk || break
                ;;
            7) edit_text sdr.flux242.script_path "Script Path" "Enter ABSOLUTE path to receivemultisonde.sh:"; chk || break ;;
            8)
                show_message "Flux242 Settings" \
"Center Frequency: $(yaml_read sdr.flux242.center_freq) Hz
Sample Rate:      $(yaml_read sdr.flux242.sample_rate) Hz
Gain:             $(yaml_read sdr.flux242.gain)
Threshold:        $(yaml_read sdr.flux242.threshold) dB
UDP Port:         $(yaml_read sdr.flux242.udp_port)
Script:           $(yaml_read sdr.flux242.script_path)"
                ;;
            B) break ;;
        esac
    done
}

configure_airspy() {
    while true; do
        CHOICE=$(whiptail --title "Airspy Configuration" --cancel-button "Main Menu" \
            --menu "Configure Airspy Mini/R2 settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Serial Number' "$(yaml_read sdr.airspy.serial)")" \
            "2" "$(mi 'Center Frequency' "$(yaml_read sdr.airspy.center_freq)")" \
            "3" "$(mi 'Sample Rate' "$(yaml_read sdr.airspy.sample_rate)")" \
            "4" "$(mi 'Decode Mode' "$(yaml_read sdr.airspy.decode_mode)")" \
            "5" "Gain Settings" \
            "6" "$(mi 'Max Channels' "$(yaml_read sdr.airspy.max_channels)")" \
            "7" "$(mi 'Detection Threshold' "$(yaml_read sdr.airspy.detection_threshold)")" \
            "8" "$(mi 'PPM Correction' "$(yaml_read sdr.airspy.ppm_correction)")" \
            "9" "View Current Settings" \
            "B" "Back to SDR Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_text sdr.airspy.serial "Serial Number" "Enter serial number (leave empty for first device):"; chk || break ;;
            2) edit_num  sdr.airspy.center_freq "Center Frequency" "Enter center frequency in Hz:"; chk || break ;;
            3) edit_choice sdr.airspy.sample_rate "Sample Rate" "Choose sample rate:" \
                    "3000000" "3 MHz (Airspy Mini)" \
                    "6000000" "6 MHz (Airspy Mini)" \
                    "2500000" "2.5 MHz (Airspy R2)" \
                    "10000000" "10 MHz (Airspy R2)"; chk || break ;;
            4) edit_choice sdr.airspy.decode_mode "Decode Mode" "Choose decode mode:" \
                    "legacy" "airspy_rx -> sox -> decoder (reliable)" \
                    "channelizer" "Python multi-channel (experimental)"; chk || break ;;
            5)
                edit_num sdr.airspy.gain "Decode Gain" "Enter decode gain (0-14):" 0 14; chk || break
                edit_num sdr.airspy.scan_gain "Scan Gain" "Enter scan gain (0-14):" 0 14; chk || break
                edit_num sdr.airspy.scan_gain_fallback "Fallback Gain" "Enter fallback scan gain (0-14):" 0 14; chk || break
                ;;
            6) edit_num  sdr.airspy.max_channels "Max Channels" "Enter max channels (1-8 for Airspy):" 1 8; chk || break ;;
            7) edit_num  sdr.airspy.detection_threshold "Detection Threshold" "Enter signal detection threshold (dB):"; chk || break ;;
            8) edit_num  sdr.airspy.ppm_correction "PPM Correction" "Enter PPM frequency correction:"; chk || break ;;
            9)
                show_message "Airspy Settings" \
"Serial:           $(yaml_read sdr.airspy.serial)
Center Frequency: $(yaml_read sdr.airspy.center_freq) Hz
Sample Rate:      $(yaml_read sdr.airspy.sample_rate) Hz
Decode Mode:      $(yaml_read sdr.airspy.decode_mode)
Gain / Scan:      $(yaml_read sdr.airspy.gain) / $(yaml_read sdr.airspy.scan_gain)
Max Channels:     $(yaml_read sdr.airspy.max_channels)
Threshold:        $(yaml_read sdr.airspy.detection_threshold) dB"
                ;;
            B) break ;;
        esac
    done
}

#==============================================================================
# Detection & Receivers Configuration
#==============================================================================

configure_detection() {
    while true; do
        CHOICE=$(whiptail --title "Detection & Receivers" --cancel-button "Main Menu" \
            --menu "Configure signal detection and receiver settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "Priority Frequency" \
            "2" "Detection Thresholds" \
            "3" "DFT Detection" \
            "4" "$(mi 'Max Concurrent Decoders' "$(yaml_read receivers.max_concurrent)")" \
            "5" "$(mi 'Receiver Bandwidth' "$(yaml_read receivers.bandwidth)")" \
            "6" "$(mi 'Scan Interval' "$(yaml_read receivers.scan_interval)")" \
            "7" "$(mi 'Min Signal Strength' "$(yaml_read receivers.min_signal_strength)")" \
            "8" "Frequency Blacklist (info)" \
            "9" "View Current Settings" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) configure_priority_freq; chk || break ;;
            2) configure_thresholds; chk || break ;;
            3) configure_dft_detection; chk || break ;;
            4) edit_num receivers.max_concurrent "Max Concurrent" "Enter max concurrent decoders per device (1 recommended):" 1 4; chk || break ;;
            5) edit_num receivers.bandwidth "Bandwidth" "Enter receiver bandwidth (Hz):"; chk || break ;;
            6) edit_num receivers.scan_interval "Scan Interval" "Enter spectrum scan interval (seconds):"; chk || break ;;
            7) edit_num receivers.min_signal_strength "Min Signal Strength" "Enter minimum SNR in dB:"; chk || break ;;
            8)
                show_message "Frequency Blacklist" \
"Current blacklist: $(yaml_read detection.frequency_blacklist)

Frequency lists (MHz) are edited directly in config.yaml
under detection.frequency_blacklist.

Example:
  frequency_blacklist: [401.90, 403.20, 405.50]"
                ;;
            9)
                show_message "Detection Settings" \
"Detection Threshold: $(yaml_read detection.detection_threshold) dB
Scan Threshold:      $(yaml_read detection.scan_threshold) dB
Scan Check Time:     $(yaml_read detection.scan_check_time) s
DC Notch:            $(yaml_read detection.dc_notch_hz) Hz
Max Concurrent:      $(yaml_read receivers.max_concurrent)
Bandwidth:           $(yaml_read receivers.bandwidth) Hz
DFT Detection:       $(yaml_read detection.use_dft_detect)
Priority Frequency:  $(yaml_read detection.priority_frequency) MHz"
                ;;
            B) break ;;
        esac
    done
}

configure_priority_freq() {
    while true; do
        CHOICE=$(whiptail --title "Priority Frequency" --cancel-button "Main Menu" \
            --menu "Configure priority frequency checking\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Priority Frequency' "$(yaml_read detection.priority_frequency)")" \
            "2" "$(mi 'Expected Sonde Type' "$(yaml_read detection.priority_sonde_type)")" \
            "3" "$(mi 'Check Timeout' "$(yaml_read detection.priority_check_timeout)")" \
            "4" "Disable Priority Check" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_num  detection.priority_frequency "Priority Frequency" "Enter priority frequency in MHz (e.g. 403.09):"; chk || break ;;
            2) edit_choice detection.priority_sonde_type "Sonde Type" "Choose expected sonde type:" \
                    "RS41" "Vaisala RS41" \
                    "RS92" "Vaisala RS92" \
                    "DFM" "Graw DFM" \
                    "M10" "Meteomodem M10" \
                    "M20" "Meteomodem M20" \
                    "iMet" "InterMet iMet" \
                    "null" "Auto-detect"; chk || break ;;
            3) edit_num  detection.priority_check_timeout "Check Timeout" "Enter check timeout (seconds):"; chk || break ;;
            4) yaml_write detection.priority_frequency "null" && show_message "Priority Check" "Priority frequency check disabled (set to null)" ;;
            B) break ;;
        esac
    done
}

configure_thresholds() {
    while true; do
        CHOICE=$(whiptail --title "Detection Thresholds" --cancel-button "Main Menu" \
            --menu "Configure signal detection thresholds\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'FFT Size' "$(yaml_read detection.fft_size)")" \
            "2" "$(mi 'Detection Threshold RTL' "$(yaml_read detection.detection_threshold)")" \
            "3" "$(mi 'Scan Threshold Airspy' "$(yaml_read detection.scan_threshold)")" \
            "4" "$(mi 'Scan Check Time' "$(yaml_read detection.scan_check_time)")" \
            "5" "$(mi 'DC Notch Width' "$(yaml_read detection.dc_notch_hz)")" \
            "6" "$(mi 'Failed Decode Cooldown' "$(yaml_read detection.failed_decode_cooldown_s)")" \
            "7" "$(mi 'RS41 Fast-Path' "$(yaml_read detection.rs41_fastpath)")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_num  detection.fft_size "FFT Size" "Enter FFT size (power of 2, e.g. 2048):"; chk || break ;;
            2) edit_num  detection.detection_threshold "Detection Threshold" "Enter detection threshold (dB above noise):"; chk || break ;;
            3) edit_num  detection.scan_threshold "Scan Threshold" "Enter Airspy scan threshold (dB):"; chk || break ;;
            4) edit_num  detection.scan_check_time "Scan Check Time" "Enter spectrum integration time per scan (seconds):"; chk || break ;;
            5) edit_num  detection.dc_notch_hz "DC Notch" "Enter DC notch width (Hz):"; chk || break ;;
            6) edit_num  detection.failed_decode_cooldown_s "Failed Decode Cooldown" "Enter failed-decode cooldown (seconds):"; chk || break ;;
            7) edit_bool detection.rs41_fastpath "RS41 Fast-Path" "Enable RS41 fast-path detection?\n(Skip DFT for RS41-width signals)"; chk || break ;;
            B) break ;;
        esac
    done
}

configure_dft_detection() {
    while true; do
        CHOICE=$(whiptail --title "DFT Detection" --cancel-button "Main Menu" \
            --menu "Configure DFT-based sonde type detection\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'DFT Detection' "$(yaml_read detection.use_dft_detect)")" \
            "2" "$(mi 'DFT Binary Path' "$(yaml_read detection.dft_detect_path)")" \
            "3" "$(mi 'Sample Duration' "$(yaml_read detection.dft_sample_duration)")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool detection.use_dft_detect "DFT Detection" "Enable DFT correlation detection?"; chk || break ;;
            2) edit_text detection.dft_detect_path "DFT Binary Path" "Enter path to dft_detect binary:"; chk || break ;;
            3) edit_num  detection.dft_sample_duration "Sample Duration" "Enter IQ sample duration for analysis (seconds):"; chk || break ;;
            B) break ;;
        esac
    done
}

#==============================================================================
# Decoders Configuration
#==============================================================================

configure_decoders() {
    while true; do
        CHOICE=$(whiptail --title "Sonde Types & Decoders" --cancel-button "Main Menu" \
            --menu "Configure decoder settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'RS1729 Path' "$(yaml_read decoders.rs1729_path)")" \
            "2" "$(mi 'Soft Decoding' "$(yaml_read decoders.soft_decode)")" \
            "3" "$(mi 'Live Signal Metrics' "$(yaml_read decoders.live_signal_metrics)")" \
            "4" "$(mi 'Startup Timeout' "$(yaml_read decoders.startup_timeout)")" \
            "5" "$(mi 'Max Idle Time' "$(yaml_read decoders.max_idle_time)")" \
            "6" "$(mi 'Manual Idle Time' "$(yaml_read decoders.manual_idle_time)")" \
            "7" "Sonde Types (info)" \
            "8" "View Current Settings" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_text decoders.rs1729_path "RS1729 Path" "Enter path to rs1729 decoder binaries:"; chk || break ;;
            2) edit_bool decoders.soft_decode "Soft Decoding" "Enable soft-decision decoding?\n(~2 dB more sensitive)"; chk || break ;;
            3) edit_bool decoders.live_signal_metrics "Live Signal Metrics" "Enable live RSSI/SNR metrics?\n(May stall under CPU load)"; chk || break ;;
            4) edit_num  decoders.startup_timeout "Startup Timeout" "Enter startup timeout (seconds):"; chk || break ;;
            5) edit_num  decoders.max_idle_time "Max Idle Time" "Enter max idle time before stopping decoder (seconds):"; chk || break ;;
            6) edit_num  decoders.manual_idle_time "Manual Idle Time" "Enter manual decoder idle time (seconds):"; chk || break ;;
            7)
                show_message "Supported Sonde Types" \
"Auto-detected sonde types are listed in config.yaml under
detection.sonde_types:

  RS41, RS92, DFM, M10, M20, iMet, LMS6, MRZ

Edit that list directly to enable/disable types."
                ;;
            8)
                show_message "Decoder Settings" \
"RS1729 Path:      $(yaml_read decoders.rs1729_path)
Soft Decode:      $(yaml_read decoders.soft_decode)
Live Metrics:     $(yaml_read decoders.live_signal_metrics)
Startup Timeout:  $(yaml_read decoders.startup_timeout) s
Max Idle Time:    $(yaml_read decoders.max_idle_time) s
Manual Idle Time: $(yaml_read decoders.manual_idle_time) s"
                ;;
            B) break ;;
        esac
    done
}

#==============================================================================
# Web UI, Map & Logging Configuration
#==============================================================================

configure_webui() {
    while true; do
        CHOICE=$(whiptail --title "Web UI & Map" --cancel-button "Main Menu" \
            --menu "Configure web interface settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Web UI Enabled' "$(yaml_read webui.enabled)")" \
            "2" "$(mi 'Host & Port' "$(yaml_read webui.host):$(yaml_read webui.port)")" \
            "3" "$(mi 'Debug Mode' "$(yaml_read webui.debug)")" \
            "4" "Map Settings" \
            "5" "$(mi 'Sonde Retention' "$(yaml_read webui.sonde_retention_time)")" \
            "6" "$(mi 'External URL Provider' "$(yaml_read webui.external_url_provider)")" \
            "7" "View Current Settings" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool webui.enabled "Web UI" "Enable the web user interface?"; chk || break ;;
            2)
                edit_text webui.host "Host" "Enter host (0.0.0.0 for all interfaces):"; chk || break
                edit_num  webui.port "Port" "Enter web UI port:"; chk || break
                ;;
            3) edit_bool webui.debug "Debug Mode" "Enable web UI debug mode?"; chk || break ;;
            4) configure_map; chk || break ;;
            5) edit_num  webui.sonde_retention_time "Sonde Retention" "Enter sonde retention time (seconds, 0 = immediate):"; chk || break ;;
            6) configure_external_url; chk || break ;;
            7)
                show_message "Web UI Settings" \
"Enabled:          $(yaml_read webui.enabled)
Host:             $(yaml_read webui.host)
Port:             $(yaml_read webui.port)
Debug:            $(yaml_read webui.debug)
Retention:        $(yaml_read webui.sonde_retention_time) s
URL Provider:     $(yaml_read webui.external_url_provider)"
                ;;
            B) break ;;
        esac
    done
}

configure_map() {
    while true; do
        CHOICE=$(whiptail --title "Map Configuration" --cancel-button "Main Menu" \
            --menu "Configure map settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Default Position' "$(yaml_read webui.map.default_lat), $(yaml_read webui.map.default_lon)")" \
            "2" "$(mi 'Default Zoom' "$(yaml_read webui.map.default_zoom)")" \
            "3" "Tile Server" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1)
                edit_text webui.map.default_lat "Latitude" "Enter default map latitude:"; chk || break
                edit_text webui.map.default_lon "Longitude" "Enter default map longitude:"; chk || break
                ;;
            2) edit_num  webui.map.default_zoom "Zoom Level" "Enter default zoom level (1-18):" 1 18; chk || break ;;
            3) edit_text webui.map.tile_server "Tile Server" "Enter tile server URL template:"; chk || break ;;
            B) break ;;
        esac
    done
}

configure_external_url() {
    edit_choice webui.external_url_provider "External URL Provider" "Choose external URL provider:" \
        "openwx" "OpenWX" \
        "sondehub" "SondeHub" \
        "custom" "Custom URL"
    chk || return 1
    if [ "$(yaml_read webui.external_url_provider)" = "custom" ]; then
        edit_text webui.external_url_custom "Custom URL" "Enter custom URL template (use <sondeid> placeholder):"
    fi
    return 0
}

configure_logging() {
    while true; do
        CHOICE=$(whiptail --title "Logging Configuration" --cancel-button "Main Menu" \
            --menu "Configure logging settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Log Level' "$(yaml_read logging.log_level)")" \
            "2" "$(mi 'Debug Mode' "$(yaml_read logging.debug_mode)")" \
            "3" "$(mi 'MQTT Debug' "$(yaml_read logging.debug_mqtt)")" \
            "4" "$(mi 'Log File Path' "$(yaml_read logging.file)")" \
            "5" "Log Rotation" \
            "6" "View Current Settings" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_choice logging.log_level "Log Level" "Choose base log level:" \
                    "DEBUG" "Detailed debug information" \
                    "INFO" "General information" \
                    "WARNING" "Warning messages" \
                    "ERROR" "Error messages only"; chk || break ;;
            2) edit_bool logging.debug_mode "Debug Mode" "Enable debug mode?\n(Show verbose scanner messages)"; chk || break ;;
            3) edit_bool logging.debug_mqtt "MQTT Debug" "Enable MQTT debug logging?"; chk || break ;;
            4) edit_text logging.file "Log File" "Enter log file path:"; chk || break ;;
            5)
                edit_num logging.max_size "Max Size" "Enter max log file size (bytes):"; chk || break
                edit_num logging.backup_count "Backup Count" "Enter number of backup log files:"; chk || break
                ;;
            6)
                show_message "Logging Settings" \
"Log Level:  $(yaml_read logging.log_level)
Debug Mode: $(yaml_read logging.debug_mode)
MQTT Debug: $(yaml_read logging.debug_mqtt)
Log File:   $(yaml_read logging.file)
Max Size:   $(yaml_read logging.max_size) bytes
Backups:    $(yaml_read logging.backup_count)"
                ;;
            B) break ;;
        esac
    done
}

#==============================================================================
# Output Configuration
#==============================================================================

configure_output() {
    while true; do
        CHOICE=$(whiptail --title "Output Configuration" --cancel-button "Main Menu" \
            --menu "Configure data output settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "UDP Output" \
            "2" "Channelizer Status" \
            "3" "$(mi 'Update Interval' "$(yaml_read output.update_interval)")" \
            "4" "View Current Settings" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) configure_udp_output; chk || break ;;
            2) configure_channelizer_status; chk || break ;;
            3) edit_num output.update_interval "Update Interval" "Enter telemetry update interval (seconds):"; chk || break ;;
            4)
                show_message "Output Settings" \
"UDP Enabled:       $(yaml_read output.udp.enabled)
UDP Host:Port:     $(yaml_read output.udp.host):$(yaml_read output.udp.port)
Channelizer:       $(yaml_read output.channelizer_status.enabled)
Channelizer Port:  $(yaml_read output.channelizer_status.port)
Update Interval:   $(yaml_read output.update_interval) s"
                ;;
            B) break ;;
        esac
    done
}

configure_udp_output() {
    while true; do
        CHOICE=$(whiptail --title "UDP Output" --cancel-button "Main Menu" \
            --menu "Configure UDP JSON output\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'UDP Output Enabled' "$(yaml_read output.udp.enabled)")" \
            "2" "$(mi 'Host' "$(yaml_read output.udp.host)")" \
            "3" "$(mi 'Port' "$(yaml_read output.udp.port)")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool output.udp.enabled "UDP Output" "Enable UDP JSON output?"; chk || break ;;
            2) edit_text output.udp.host "UDP Host" "Enter UDP host:"; chk || break ;;
            3) edit_num  output.udp.port "UDP Port" "Enter UDP port:"; chk || break ;;
            B) break ;;
        esac
    done
}

configure_channelizer_status() {
    while true; do
        CHOICE=$(whiptail --title "Channelizer Status" --cancel-button "Main Menu" \
            --menu "Configure channelizer status output\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Status Enabled' "$(yaml_read output.channelizer_status.enabled)")" \
            "2" "$(mi 'Host' "$(yaml_read output.channelizer_status.host)")" \
            "3" "$(mi 'Port' "$(yaml_read output.channelizer_status.port)")" \
            "4" "$(mi 'Update Interval' "$(yaml_read output.channelizer_status.update_interval)")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool output.channelizer_status.enabled "Channelizer Status" "Enable channelizer status output?"; chk || break ;;
            2) edit_text output.channelizer_status.host "Host" "Enter status output host:"; chk || break ;;
            3) edit_num  output.channelizer_status.port "Port" "Enter status output port:"; chk || break ;;
            4) edit_num  output.channelizer_status.update_interval "Update Interval" "Enter status update interval (seconds):"; chk || break ;;
            B) break ;;
        esac
    done
}

#==============================================================================
# Upload & Import Configuration
#==============================================================================

configure_upload() {
    while true; do
        CHOICE=$(whiptail --title "Upload Settings" --cancel-button "Main Menu" \
            --menu "Configure upload settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "OpenWX Upload" \
            "2" "$(mi 'SondeHub Upload' "$(yaml_read sondehub.enabled)")" \
            "3" "View Current Settings" \
            "B" "Back to Main Menu" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) configure_openwx; chk || break ;;
            2) configure_sondehub; chk || break ;;
            3)
                show_message "Upload Status" \
"OpenWX MQTT: $(yaml_read openwx.mqtt.enabled)
OpenWX HTTP: $(yaml_read openwx.http.enabled)
SondeHub:    $(yaml_read sondehub.enabled)"
                ;;
            B) break ;;
        esac
    done
}

configure_openwx() {
    while true; do
        CHOICE=$(whiptail --title "OpenWX Upload" --cancel-button "Main Menu" \
            --menu "Configure OpenWX upload settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "MQTT Upload" \
            "2" "HTTP Upload" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) configure_openwx_mqtt; chk || break ;;
            2) configure_openwx_http; chk || break ;;
            B) break ;;
        esac
    done
}

configure_openwx_mqtt() {
    while true; do
        CHOICE=$(whiptail --title "OpenWX MQTT" --cancel-button "Main Menu" \
            --menu "Configure OpenWX MQTT upload\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'MQTT Enabled' "$(yaml_read openwx.mqtt.enabled)")" \
            "2" "$(mi 'Server & Port' "$(yaml_read openwx.mqtt.server):$(yaml_read openwx.mqtt.port)")" \
            "3" "Credentials" \
            "4" "Topic & Client ID" \
            "5" "$(mi 'TLS Enabled' "$(yaml_read openwx.mqtt.tls_enabled)")" \
            "6" "View Settings" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool openwx.mqtt.enabled "OpenWX MQTT" "Enable OpenWX MQTT upload?"; chk || break ;;
            2)
                edit_text openwx.mqtt.server "MQTT Server" "Enter MQTT broker hostname or IP:"; chk || break
                edit_num  openwx.mqtt.port "MQTT Port" "Enter MQTT broker port (1883 = plain, 8883 = TLS):"; chk || break
                ;;
            3)
                edit_text openwx.mqtt.username "Username" "Enter MQTT username:"; chk || break
                MQTT_PASS=$(whiptail --title "Password" --cancel-button "Main Menu" \
                    --passwordbox "Enter MQTT password (leave blank to keep current):" $WT_HEIGHT $WT_WIDTH 3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }
                [ -n "$MQTT_PASS" ] && { yaml_write openwx.mqtt.password "$MQTT_PASS" && show_message "Password" "MQTT password updated"; }
                ;;
            4)
                edit_text openwx.mqtt.topic_prefix "Topic Prefix" "Enter MQTT topic prefix:"; chk || break
                edit_text openwx.mqtt.client_id "Client ID" "Enter MQTT client ID:"; chk || break
                ;;
            5) edit_bool openwx.mqtt.tls_enabled "TLS" "Enable TLS encryption?"; chk || break ;;
            6)
                show_message "OpenWX MQTT Settings" \
"Enabled:  $(yaml_read openwx.mqtt.enabled)
Server:   $(yaml_read openwx.mqtt.server):$(yaml_read openwx.mqtt.port)
Username: $(yaml_read openwx.mqtt.username)
Topic:    $(yaml_read openwx.mqtt.topic_prefix)
ClientID: $(yaml_read openwx.mqtt.client_id)
TLS:      $(yaml_read openwx.mqtt.tls_enabled)"
                ;;
            B) break ;;
        esac
    done
}

configure_openwx_http() {
    while true; do
        CHOICE=$(whiptail --title "OpenWX HTTP" --cancel-button "Main Menu" \
            --menu "Configure OpenWX HTTP upload\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'HTTP Enabled' "$(yaml_read openwx.http.enabled)")" \
            "2" "$(mi 'Upload URL' "$(yaml_read openwx.http.url)")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool openwx.http.enabled "OpenWX HTTP" "Enable OpenWX HTTP upload?"; chk || break ;;
            2) edit_text openwx.http.url "Upload URL" "Enter OpenWX HTTP upload URL:"; chk || break ;;
            B) break ;;
        esac
    done
}

configure_sondehub() {
    while true; do
        CHOICE=$(whiptail --title "SondeHub Upload" --cancel-button "Main Menu" \
            --menu "Configure SondeHub upload settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'SondeHub Enabled' "$(yaml_read sondehub.enabled)")" \
            "2" "$(mi 'Station ID' "$(yaml_read sondehub.station_id)")" \
            "3" "$(mi 'Uploader Callsign' "$(yaml_read sondehub.uploader_callsign)")" \
            "4" "Station Details" \
            "5" "$(mi 'Queue Mode' "$(yaml_read sondehub.queue_mode)")" \
            "6" "$(mi 'Upload Rate' "$(yaml_read sondehub.upload_rate_s)")" \
            "7" "View Settings" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool sondehub.enabled "SondeHub" "Enable SondeHub upload?"; chk || break ;;
            2) edit_text sondehub.station_id "Station ID" "Enter SondeHub station ID:"; chk || break ;;
            3) edit_text sondehub.uploader_callsign "Callsign" "Enter uploader callsign:"; chk || break ;;
            4)
                edit_text sondehub.uploader_antenna "Antenna" "Enter antenna description:"; chk || break
                edit_text sondehub.uploader_radio "Radio" "Enter radio/receiver description:"; chk || break
                edit_text sondehub.contact_email "Email" "Enter contact email (optional):"; chk || break
                ;;
            5) edit_bool sondehub.queue_mode "Queue Mode" "Enable queued batch upload mode?\n(No = direct upload)"; chk || break ;;
            6) edit_num  sondehub.upload_rate_s "Upload Rate" "Enter upload rate (seconds, 1-10 recommended):"; chk || break ;;
            7)
                show_message "SondeHub Settings" \
"Enabled:    $(yaml_read sondehub.enabled)
Station ID: $(yaml_read sondehub.station_id)
Callsign:   $(yaml_read sondehub.uploader_callsign)
Antenna:    $(yaml_read sondehub.uploader_antenna)
Radio:      $(yaml_read sondehub.uploader_radio)
Queue Mode: $(yaml_read sondehub.queue_mode)
Rate:       $(yaml_read sondehub.upload_rate_s) s"
                ;;
            B) break ;;
        esac
    done
}

configure_import_api() {
    while true; do
        CHOICE=$(whiptail --title "Import API" --cancel-button "Main Menu" \
            --menu "Configure Import API settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Import API Enabled' "$(yaml_read import_api.enabled)")" \
            "2" "$(mi 'API URL' "$(yaml_read import_api.url)")" \
            "3" "$(mi 'Check Interval' "$(yaml_read import_api.check_interval_s)")" \
            "4" "Search Parameters" \
            "5" "$(mi 'Max Sondes' "$(yaml_read import_api.max_sondes)")" \
            "6" "Cooldowns" \
            "7" "View Settings" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_bool import_api.enabled "Import API" "Enable Import API for automatic sonde detection?"; chk || break ;;
            2) edit_text import_api.url "API URL" "Enter Import API URL:"; chk || break ;;
            3) edit_num  import_api.check_interval_s "Check Interval" "Enter check interval (seconds):"; chk || break ;;
            4)
                edit_num import_api.distance_km "Distance" "Enter search distance (km):"; chk || break
                edit_num import_api.time_range_minutes "Time Range" "Enter time range (minutes):"; chk || break
                ;;
            5) edit_num  import_api.max_sondes "Max Sondes" "Enter max sondes to import:"; chk || break ;;
            6) configure_import_cooldowns; chk || break ;;
            7)
                show_message "Import API Settings" \
"Enabled:      $(yaml_read import_api.enabled)
URL:          $(yaml_read import_api.url)
Interval:     $(yaml_read import_api.check_interval_s) s
Distance:     $(yaml_read import_api.distance_km) km
Time Range:   $(yaml_read import_api.time_range_minutes) min
Max Sondes:   $(yaml_read import_api.max_sondes)"
                ;;
            B) break ;;
        esac
    done
}

configure_import_cooldowns() {
    while true; do
        CHOICE=$(whiptail --title "Import Cooldowns" --cancel-button "Main Menu" \
            --menu "Configure import cooldown settings\n" $WT_HEIGHT $WT_WIDTH $WT_MENU_HEIGHT \
            "1" "$(mi 'Re-assign Cooldown' "$(yaml_read import_api.reassign_cooldown_s)")" \
            "2" "$(mi 'Landed Altitude' "$(yaml_read import_api.landed_alt_m)")" \
            "3" "$(mi 'Landed Cooldown' "$(yaml_read import_api.landed_cooldown_s)")" \
            "B" "Back" \
            3>&1 1>&2 2>&3) || { RETURN_TO_MAIN=1; break; }

        case $CHOICE in
            1) edit_num import_api.reassign_cooldown_s "Re-assign Cooldown" "Enter re-assign cooldown (seconds):"; chk || break ;;
            2) edit_num import_api.landed_alt_m "Landed Altitude" "Enter landed altitude threshold (metres):"; chk || break ;;
            3) edit_num import_api.landed_cooldown_s "Landed Cooldown" "Enter landed sonde cooldown (seconds):"; chk || break ;;
            B) break ;;
        esac
    done
}

#==============================================================================
# About
#==============================================================================

show_about() {
    whiptail --title "About OpenWXSDR" --msgbox \
"$LOGO_BANNER

$(center_line "Version $APP_VERSION")" $WT_HEIGHT $WT_WIDTH
}

#==============================================================================
# Config Check
#==============================================================================

# Normalised key lists (array index [n] collapsed to []) for comparison.
_cfg_missing_keys() {
    comm -23 \
        <(python3 "$PYTOOL" getall "$1" 2>/dev/null | cut -f1 | sed -E 's/\[[0-9]+\]/[]/g' | sort -u) \
        <(python3 "$PYTOOL" getall "$CONFIG_FILE" 2>/dev/null | cut -f1 | sed -E 's/\[[0-9]+\]/[]/g' | sort -u)
}
_cfg_extra_keys() {
    comm -13 \
        <(python3 "$PYTOOL" getall "$1" 2>/dev/null | cut -f1 | sed -E 's/\[[0-9]+\]/[]/g' | sort -u) \
        <(python3 "$PYTOOL" getall "$CONFIG_FILE" 2>/dev/null | cut -f1 | sed -E 's/\[[0-9]+\]/[]/g' | sort -u)
}

# Compare config.yaml against config.yaml.example. Missing settings can be
# copied in individually (tick a line) or all at once ([*] Fix all).
config_check() {
    local example="$PROJECT_ROOT/config.yaml.example"
    if [ ! -f "$example" ]; then
        show_error "Reference file not found:\n$example"
        return
    fi

    while true; do
        local -a missing=() extra=()
        local k
        while IFS= read -r k; do [ -n "$k" ] && missing+=("$k"); done < <(_cfg_missing_keys "$example")
        while IFS= read -r k; do [ -n "$k" ] && extra+=("$k"); done < <(_cfg_extra_keys "$example")

        if [ ${#missing[@]} -eq 0 ]; then
            show_message "Config Check" \
"OK: no settings are missing.

All keys from config.yaml.example are present in your config.yaml.

Extra keys in config.yaml not in the example: ${#extra[@]}"
            return
        fi

        # Build a checklist: [*] Fix all, then one selectable line per missing
        # key. Descriptions are left empty so a long key name can never overlap
        # a second text column; the visible label is the tag itself.
        local fixall_tag="[*] Fix all / add missing setting"
        local -a items=()
        items+=("$fixall_tag" "" "OFF")
        for k in "${missing[@]}"; do
            items+=("$k" "" "OFF")
        done

        local selected
        selected=$(whiptail --title "Config Check - ${#missing[@]} missing" --cancel-button "Main Menu" \
            --checklist "Tick settings to copy from config.yaml.example into config.yaml:" \
            $WT_HEIGHT $WT_WIDTH 12 "${items[@]}" 3>&1 1>&2 2>&3) || return

        [ -z "$selected" ] && return

        # whiptail returns ticked tags shell-quoted and space-separated; eval
        # into an array so the multi-word Fix-all label stays a single element.
        local -a sel=()
        eval "sel=($selected)"
        local -a fixlist=()
        local t fixall=0
        for t in "${sel[@]}"; do
            if [ "$t" = "$fixall_tag" ]; then fixall=1; else fixlist+=("$t"); fi
        done
        [ $fixall -eq 1 ] && fixlist=("${missing[@]}")
        [ ${#fixlist[@]} -eq 0 ] && return

        # Back up before modifying, then copy each selected setting from example.
        backup_config >/dev/null 2>&1
        local applied=0 failed=0 fk
        for fk in "${fixlist[@]}"; do
            if python3 "$PYTOOL" fixkey "$example" "$CONFIG_FILE" "$fk" 2>/dev/null; then
                applied=$((applied + 1))
            else
                failed=$((failed + 1))
            fi
        done
        load_cache
        show_message "Config Check" \
"Added $applied setting(s) from config.yaml.example.
Failed: $failed

A backup of the previous config.yaml was saved in:
$BACKUP_DIR"
        # Loop re-runs the check so remaining missing keys are shown.
    done
}

#==============================================================================
# Main Menu
#==============================================================================

main_menu() {
    while true; do
        RETURN_TO_MAIN=0
        CHOICE=$(whiptail --title "OpenWXSDR Configuration" --cancel-button "Exit" \
            --menu "Configure OpenWXSDR settings\n" $WT_HEIGHT $WT_WIDTH 13 \
            "1" "Station Configuration" \
            "2" "SDR Configuration" \
            "3" "Detection & Receivers" \
            "4" "Sonde Types & Decoders" \
            "5" "Web UI & Map" \
            "6" "Logging Configuration" \
            "7" "Output Configuration" \
            "8" "Upload settings" \
            "9" "Import configuration" \
            "" "" \
            "A" "About" \
            "B" "Backup config.yaml" \
            "C" "Config check" \
            3>&1 1>&2 2>&3) || exit 0

        case $CHOICE in
            1) configure_station ;;
            2) configure_sdr ;;
            3) configure_detection ;;
            4) configure_decoders ;;
            5) configure_webui ;;
            6) configure_logging ;;
            7) configure_output ;;
            8) configure_upload ;;
            9) configure_import_api ;;
            A) show_about ;;
            B) backup_config; show_message "Backup" "Configuration backup created in:\n$BACKUP_DIR" ;;
            C) config_check ;;
        esac
    done
}

#==============================================================================
# Main Execution
#==============================================================================

check_dependencies

if [ ! -f "$CONFIG_FILE" ]; then
    whiptail --title "Error" --msgbox "Config file not found:\n$CONFIG_FILE\n\nPlease create config.yaml from config.yaml.example" $WT_HEIGHT $WT_WIDTH
    exit 1
fi

if whiptail --title "OpenWXSDR Configuration Tool" \
    --yes-button "Start" --no-button "Exit" --yesno \
"$LOGO_BANNER

$(center_line "Version $APP_VERSION")

Config file: $CONFIG_FILE
Backups:     $BACKUP_DIR" $WT_HEIGHT $WT_WIDTH; then
    backup_config
    load_cache
    main_menu
else
    exit 0
fi
