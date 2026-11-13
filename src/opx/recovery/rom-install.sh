#!/sbin/sh
# OPxFlasher full ROM installer for TWRP-based recoveries (OrangeFox, TWRP, AERA).
#
# Flashes a full ROM converted by OPxFlasher to the current slot: firmware and
# boot images to their partitions, logical partitions (system, vendor, odm,
# my_*, ...) resized inside super with lptools and written, vbmeta last.
#
# Before anything is written it checks, and stops if any check fails:
#   - the phone is the board the ROM is for, and the same variant (no region
#     change: that needs a data wipe, which this installer never does)
#   - the ROM is not older than the installed Android (anti-rollback fuse)
#   - no system update is half-applied (snapshot merge)
#   - every partition exists and is big enough; lptools is there
#   - every file in the zip matches its SHA-256
# Afterwards it reads everything back. Never touches userdata, metadata,
# persist or other identity partitions (they aren't in the zip).
#
# update-binary arguments: <api> <outfd> <zip>

OUTFD=$2
ZIP=$3
BLOCK=${OPX_BLOCK_DIR:-/dev/block/by-name}
MAPPER=${OPX_MAPPER_DIR:-/dev/block/mapper}
GETPROP=${OPX_GETPROP:-getprop}
LPTOOLS=${OPX_LPTOOLS:-lptools}
METADATA=${OPX_METADATA:-/metadata}
TMP=${OPX_TMP:-/tmp}/opx-rom
MB=1048576

ui() {
  if [ -n "$OUTFD" ] && [ -e "/proc/self/fd/$OUTFD" ]; then
    echo "ui_print $1" > "/proc/self/fd/$OUTFD"
    echo "ui_print" > "/proc/self/fd/$OUTFD"
  else
    echo "$1"
  fi
}

cleanup() { umount "$TMP/sys" >/dev/null 2>&1; rm -rf "$TMP"; }

abort() {
  ui " "
  ui "! $1"
  ui "! Nothing was written."
  cleanup
  exit 1
}

fail_written() {
  ui " "
  ui "! $1"
  ui "! The phone is half flashed. Do NOT reboot: flash this zip again,"
  ui "! or flash the ROM from the PC with OPxFlasher."
  cleanup
  exit 1
}

prop() { "$GETPROP" "$1" 2>/dev/null; }

sha_of_entry() { unzip -p "$ZIP" "$1" 2>/dev/null | sha256sum | cut -d' ' -f1; }

# value of key=... in a build.prop
bprop() { sed -n "s/^$1=//p" "$TMP/installed.prop" | head -n 1; }

devsize() {
  if [ -f "$1" ]; then
    wc -c < "$1" | tr -d ' '
    return
  fi
  b=$(basename "$(readlink -f "$1")")
  s=$(cat "/sys/class/block/$b/size" 2>/dev/null)
  [ -n "$s" ] && echo $((s * 512))
}

ui "OPxFlasher full ROM installer"
ui "-----------------------------"

for tool in unzip sha256sum dd cut head sed grep tr; do
  command -v "$tool" >/dev/null 2>&1 || abort "This recovery has no '$tool'. Use OrangeFox, or flash from the PC."
done

rm -rf "$TMP"
mkdir -p "$TMP" || abort "Can't create $TMP."
unzip -p "$ZIP" opx/rom.list > "$TMP/list" 2>/dev/null
[ -s "$TMP/list" ] || abort "This zip has no ROM list. Is it damaged?"

value() { sed -n "s/^$1 //p" "$TMP/list" | head -n 1; }
PRODUCT=$(value product)
BUILD=$(value build)
CHUNK=$(value chunk)
ROM_TS=$(value timestamp)
ROM_PATCH=$(value patch)
ROM_FP=$(value fingerprint)
ui "ROM: $PRODUCT $BUILD"

# --- 1. right phone ------------------------------------------------------------
ok=""
for p in ro.product.device ro.build.product ro.product.board ro.product.vendor.device; do
  have=$(prop "$p")
  [ -n "$have" ] || continue
  for b in $(sed -n 's/^board //p' "$TMP/list"); do
    [ "$have" = "$b" ] && ok=1
  done
done
[ -n "$ok" ] || abort "This ROM is for $PRODUCT ($(sed -n 's/^board //p' "$TMP/list" | tr '\n' ' ')); this phone is '$(prop ro.product.device)'."

slot=$(prop ro.boot.slot_suffix)
case "$slot" in _a|_b) ;; *) abort "Can't tell which slot is active." ;; esac
ui "- Phone: $(prop ro.product.device), slot ${slot#_}"

# --- 2. nothing half-updated ---------------------------------------------------------
mount "$METADATA" >/dev/null 2>&1
if [ -d "$METADATA/ota/snapshots" ] && [ -n "$(ls -A "$METADATA/ota/snapshots" 2>/dev/null)" ]; then
  abort "A system update is still being applied (merge in progress). Boot Android, let it finish, then try again."
fi

# --- 3. what's installed: same variant, not newer than this ROM -------------------------
if [ -n "$OPX_SYSTEM_PROP" ]; then
  cat "$OPX_SYSTEM_PROP" > "$TMP/installed.prop" 2>/dev/null
else
  mkdir -p "$TMP/sys"
  for dev in "$MAPPER/system$slot" "$BLOCK/system$slot"; do
    [ -e "$dev" ] || continue
    mount -o ro "$dev" "$TMP/sys" >/dev/null 2>&1 \
      || mount -t erofs -o ro "$dev" "$TMP/sys" >/dev/null 2>&1 \
      || mount -t ext4 -o ro "$dev" "$TMP/sys" >/dev/null 2>&1 || continue
    for f in "$TMP/sys/system/build.prop" "$TMP/sys/build.prop"; do
      [ -f "$f" ] && { cat "$f" > "$TMP/installed.prop"; break; }
    done
    umount "$TMP/sys" >/dev/null 2>&1
    [ -s "$TMP/installed.prop" ] && break
  done
fi
[ -s "$TMP/installed.prop" ] || abort "Can't read the installed Android's build (system$slot), so a downgrade can't be ruled out. Flash from the PC with OPxFlasher instead."

HAVE_FP=$(bprop ro.system.build.fingerprint)
[ -n "$HAVE_FP" ] || HAVE_FP=$(bprop ro.build.fingerprint)
HAVE_TS=$(bprop ro.system.build.date.utc)
[ -n "$HAVE_TS" ] || HAVE_TS=$(bprop ro.build.date.utc)
HAVE_PATCH=$(bprop ro.build.version.security_patch)
HAVE_PRODUCT=$(echo "$HAVE_FP" | cut -d/ -f2)
[ -n "$HAVE_PRODUCT" ] || abort "Can't read the installed Android's model, so a region change can't be ruled out."
[ "$HAVE_PRODUCT" = "$PRODUCT" ] || abort "This ROM is for $PRODUCT but the phone runs $HAVE_PRODUCT. Changing region needs a data wipe; use OPxFlasher on the PC."
ui "- Same variant: $PRODUCT"

num() { echo "$1" | tr -cd '0-9'; }
newer=""
if [ -n "$ROM_TS" ] && [ -n "$(num "$HAVE_TS")" ]; then
  [ "$(num "$ROM_TS")" -ge "$(num "$HAVE_TS")" ] && newer=1
  [ -n "$newer" ] || abort "This ROM is OLDER than the installed Android. Installing older software can blow the anti-rollback fuse and brick the phone for good. Stopped."
elif [ -n "$ROM_PATCH" ] && [ -n "$(num "$HAVE_PATCH")" ]; then
  if [ "$(num "$ROM_PATCH")" -gt "$(num "$HAVE_PATCH")" ]; then
    newer=1
  elif [ "$(num "$ROM_PATCH")" -eq "$(num "$HAVE_PATCH")" ] && [ -n "$ROM_FP" ] && [ "$ROM_FP" = "$HAVE_FP" ]; then
    newer=1
  fi
  [ -n "$newer" ] || abort "Can't confirm this ROM is not older than the installed Android (anti-rollback fuse). Flash from the PC with OPxFlasher instead."
else
  abort "Can't compare this ROM's age with the installed Android (anti-rollback fuse). Flash from the PC with OPxFlasher instead."
fi
ui "- Not older than the installed Android: yes"

# --- 4. every target exists and is big enough ------------------------------------------
: > "$TMP/targets"
needs_lp=""
while read -r tag kind name size sha; do
  [ "$tag" = image ] || continue
  dev=""
  if [ "$kind" != logical ]; then
    for d in "$BLOCK/$name$slot" "$BLOCK/$name"; do
      [ -e "$d" ] && { dev=$d; break; }
    done
  fi
  if [ -n "$dev" ]; then
    have=$(devsize "$dev")
    [ -n "$have" ] || abort "Can't read the size of $name on this phone."
    [ "$have" -ge "$size" ] || abort "$name ($size bytes) doesn't fit this phone's partition ($have bytes). Wrong ROM?"
    echo "physical $name $dev $size" >> "$TMP/targets"
  elif [ "$kind" = physical ]; then
    abort "Partition $name doesn't exist on this phone. Wrong ROM?"
  else
    [ "$kind" = logical ] || [ -e "$MAPPER/$name$slot" ] \
      || abort "$name isn't a partition on this phone. Wrong ROM?"
    echo "$kind $name $MAPPER/$name$slot $size" >> "$TMP/targets"
    needs_lp=1
  fi
done < "$TMP/list"
if [ -n "$needs_lp" ]; then
  command -v "$LPTOOLS" >/dev/null 2>&1 || abort "This recovery has no lptools, which is needed for system, vendor, odm, ... Use OrangeFox or AERA, or flash from the PC."
fi
ui "- Partitions: all present"

# --- 5. verify every file in the zip ---------------------------------------------------
ui "- Checking the zip (takes a few minutes)..."
while read -r tag name index entry sha len; do
  [ "$tag" = part ] || continue
  [ "$(sha_of_entry "$entry")" = "$sha" ] || abort "$entry in the zip is damaged. Copy the zip to the phone again."
done < "$TMP/list"
ui "- Zip checked: all files intact"

# --- 6. write --------------------------------------------------------------------------
for m in /system_root /system /vendor /product /odm /system_ext; do umount "$m" >/dev/null 2>&1; done

write_parts() { # name device
  per=$((CHUNK / MB))
  grep "^part $1 " "$TMP/list" | while read -r tag name index entry sha len; do
    unzip -p "$ZIP" "$entry" | dd of="$2" bs=$MB seek=$((index * per)) conv=notrunc,fsync 2>/dev/null || exit 1
  done
}

is_avb() { case "$1" in vbmeta|vbmeta_system|vbmeta_vendor) return 0 ;; esac; return 1; }

ui "- Writing firmware and boot images to slot ${slot#_}..."
while read -r kind name dev size; do
  [ "$kind" = physical ] || continue
  is_avb "$name" && continue
  ui "  $name"
  write_parts "$name" "$dev" || fail_written "Writing $name failed."
done < "$TMP/targets"

ui "- Writing system, vendor, odm, ... inside super..."
while read -r kind name dev size; do
  [ "$kind" = physical ] && continue
  ui "  $name"
  "$LPTOOLS" unmap "$name$slot" >/dev/null 2>&1
  if ! "$LPTOOLS" resize "$name$slot" "$size" >/dev/null 2>&1; then
    [ "$kind" = logical ] && "$LPTOOLS" create "$name$slot" "$size" >/dev/null 2>&1 \
      || fail_written "Can't make room for $name inside super."
  fi
  "$LPTOOLS" map "$name$slot" >/dev/null 2>&1
  [ -e "$dev" ] || fail_written "$name$slot isn't available after resizing."
  write_parts "$name" "$dev" || fail_written "Writing $name failed."
done < "$TMP/targets"

ui "- Writing vbmeta..."
while read -r kind name dev size; do
  [ "$kind" = physical ] && is_avb "$name" || continue
  ui "  $name"
  write_parts "$name" "$dev" || fail_written "Writing $name failed."
done < "$TMP/targets"
sync

# --- 7. read back --------------------------------------------------------------------
ui "- Reading back what was written..."
while read -r kind name dev size; do
  want=$(sed -n "s/^image [a-z]* $name $size //p" "$TMP/list" | head -n 1)
  got=$(dd if="$dev" bs=$MB count=$(((size + MB - 1) / MB)) 2>/dev/null | head -c "$size" | sha256sum | cut -d' ' -f1)
  [ "$got" = "$want" ] || fail_written "$name doesn't match after writing."
done < "$TMP/targets"

cleanup
ui " "
ui "Done: $PRODUCT $BUILD is installed. Your data was not touched."
ui "Reboot to system. The first boot can take a few minutes."
exit 0
