#!/sbin/sh
# OPxFlasher full ROM restore for TWRP-based recoveries (OrangeFox, TWRP).
#
# Writes the backup's own firmware images to the current slot and its whole
# super partition back. Every check runs, and every file in the zip is
# verified, before anything is written. Never touches userdata, metadata,
# persist, modem or other identity partitions (they aren't in the zip).
#
# update-binary arguments: <api> <outfd> <zip>

OUTFD=$2
ZIP=$3
BLOCK=${OPX_BLOCK_DIR:-/dev/block/by-name}
GETPROP=${OPX_GETPROP:-getprop}
CMDLINE=${OPX_CMDLINE:-/proc/cmdline}
METADATA=${OPX_METADATA:-/metadata}
TMP=${OPX_TMP:-/tmp}/opx-restore
MB=1048576

ui() {
  if [ -n "$OUTFD" ] && [ -e "/proc/self/fd/$OUTFD" ]; then
    echo "ui_print $1" > "/proc/self/fd/$OUTFD"
    echo "ui_print" > "/proc/self/fd/$OUTFD"
  else
    echo "$1"
  fi
}

abort() {
  ui " "
  ui "! $1"
  ui "! Nothing was written."
  rm -rf "$TMP"
  exit 1
}

fail_written() {
  ui " "
  ui "! $1"
  ui "! The phone is half restored. Do NOT reboot: flash this zip again,"
  ui "! or restore from the PC with OPxFlasher (fastboot)."
  rm -rf "$TMP"
  exit 1
}

prop() { "$GETPROP" "$1" 2>/dev/null; }

sha_of_entry() { unzip -p "$ZIP" "$1" 2>/dev/null | sha256sum | cut -d' ' -f1; }

ui "OPxFlasher full ROM restore"
ui "---------------------------"

for tool in unzip sha256sum dd cut; do
  command -v "$tool" >/dev/null 2>&1 || abort "This recovery has no '$tool'. Use OrangeFox, or restore from the PC."
done

rm -rf "$TMP"
mkdir -p "$TMP" || abort "Can't create $TMP."
unzip -p "$ZIP" opx/restore.list > "$TMP/list" 2>/dev/null
[ -s "$TMP/list" ] || abort "This zip has no restore list. Is it damaged?"

value() { sed -n "s/^$1 //p" "$TMP/list" | head -n 1; }
WANT_SERIAL=$(value serial)
WANT_SLOT=$(value slot)
MODEL=$(value model)
BUILD=$(value build)
CHUNK=$(value chunk)
SUPER_SIZE=$(value supersize)
ui "Backup: $MODEL $BUILD, slot ${WANT_SLOT#_}"

# --- 1. same phone -----------------------------------------------------------
ok=""
for p in ro.product.device ro.build.product ro.product.board ro.product.vendor.device; do
  have=$(prop "$p")
  [ -n "$have" ] || continue
  for b in $(sed -n 's/^board //p' "$TMP/list"); do
    [ "$have" = "$b" ] && ok=1
  done
done
[ -n "$ok" ] || abort "This zip is for $MODEL ($(sed -n 's/^board //p' "$TMP/list" | tr '\n' ' ')); this phone is '$(prop ro.product.device)'."

serial=$(prop ro.serialno)
[ -n "$serial" ] || serial=$(prop ro.boot.serialno)
if [ -z "$serial" ] && [ -r "$CMDLINE" ]; then
  serial=$(tr ' ' '\n' < "$CMDLINE" | sed -n 's/^androidboot.serialno=//p' | head -n 1)
fi
[ -n "$serial" ] || abort "Can't read this phone's serial number, so can't tell it's the phone the backup came from."
[ "$serial" = "$WANT_SERIAL" ] || abort "This zip was made from another phone's backup. It only restores the phone it came from."
ui "- Same phone: yes"

slot=$(prop ro.boot.slot_suffix)
[ "$slot" = "$WANT_SLOT" ] || abort "The backup was taken on slot ${WANT_SLOT#_} but slot ${slot#_} is active. Switch slot (Reboot menu), reboot to recovery and flash again."
ui "- Active slot: ${slot#_}"

# --- 2. nothing half-updated ---------------------------------------------------
mount "$METADATA" >/dev/null 2>&1
if [ -d "$METADATA/ota/snapshots" ] && [ -n "$(ls -A "$METADATA/ota/snapshots" 2>/dev/null)" ]; then
  abort "A system update is still being applied (merge in progress). Boot Android, let it finish, then try again."
fi

# --- 3. targets exist; firmware unchanged since the backup (anti-rollback) ---
while read -r kind name entry sha flag; do
  [ "$kind" = image ] || continue
  [ -e "$BLOCK/$name$slot" ] || abort "Partition $name$slot doesn't exist on this phone."
  if [ "$flag" = firmware ]; then
    now=$(sha256sum < "$BLOCK/$name$slot" | cut -d' ' -f1)
    [ "$now" = "$sha" ] || abort "The phone's $name changed since this backup (an update?). Restoring older firmware could trip anti-rollback and brick it. Make a new backup, or restore from the PC with OPxFlasher."
  fi
done < "$TMP/list"
[ -e "$BLOCK/super" ] || abort "This phone has no super partition."
ui "- Firmware unchanged since the backup: yes"

# --- 4. verify every file in the zip ---------------------------------------------
ui "- Checking the zip (takes a few minutes)..."
while read -r kind name entry sha flag; do
  case "$kind" in image|super) ;; *) continue ;; esac
  [ "$(sha_of_entry "$entry")" = "$sha" ] || abort "$entry in the zip is damaged. Copy the zip to the phone again."
done < "$TMP/list"
ui "- Zip checked: all files intact"

# --- 5. write ------------------------------------------------------------------
for m in /system_root /system /vendor /product /odm /system_ext; do umount "$m" >/dev/null 2>&1; done
ui "- Writing firmware to slot ${slot#_}..."
while read -r kind name entry sha flag; do
  [ "$kind" = image ] || continue
  ui "  $name$slot"
  unzip -p "$ZIP" "$entry" | dd of="$BLOCK/$name$slot" bs=$MB conv=notrunc,fsync 2>/dev/null \
    || fail_written "Writing $name$slot failed."
done < "$TMP/list"

ui "- Writing super (system, vendor, product, ...)..."
per=$((CHUNK / MB))
while read -r kind index entry sha flag; do
  [ "$kind" = super ] || continue
  unzip -p "$ZIP" "$entry" | dd of="$BLOCK/super" bs=$MB seek=$((index * per)) conv=notrunc,fsync 2>/dev/null \
    || fail_written "Writing super part $index failed."
done < "$TMP/list"
sync

# --- 6. read back --------------------------------------------------------------
ui "- Reading back what was written..."
while read -r kind name entry sha flag; do
  [ "$kind" = image ] || continue
  [ "$(sha256sum < "$BLOCK/$name$slot" | cut -d' ' -f1)" = "$sha" ] || fail_written "$name$slot doesn't match after writing."
done < "$TMP/list"
while read -r kind index entry sha flag; do
  [ "$kind" = super ] || continue
  len=$(sed -n "s/^size $entry //p" "$TMP/list")
  got=$(dd if="$BLOCK/super" bs=$MB skip=$((index * per)) count=$(((len + MB - 1) / MB)) 2>/dev/null \
        | head -c "$len" | sha256sum | cut -d' ' -f1)
  [ "$got" = "$sha" ] || fail_written "super part $index doesn't match after writing."
done < "$TMP/list"

rm -rf "$TMP"
ui " "
ui "Restore finished. Your data was not touched."
ui "Reboot to system. The first boot can take a few minutes."
exit 0
