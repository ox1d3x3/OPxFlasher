#!/sbin/sh
# OPxFlasher full ROM installer for TWRP-based recoveries (OrangeFox, TWRP, AERA).
#
# Flashes a full ROM converted by OPxFlasher to the current slot: firmware and
# boot images to their partitions, logical partitions (system, vendor, odm,
# my_*, ...) resized inside super with lptools and written, vbmeta last.
#
# Before anything is written it checks, and stops if any check fails:
#   - the phone is the board the ROM is for, and the same variant. A region
#     change is only allowed by a "convert ready" zip, made by OPxFlasher from
#     this very phone's backup (its serial must match): that zip carries the
#     phone's own network files and formats data at the end.
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
TWRP=${OPX_TWRP:-twrp}
GETEVENT=${OPX_GETEVENT:-getevent}
KEY_WAIT=${OPX_KEY_WAIT:-30}
TIMEOUT=${OPX_TIMEOUT:-timeout}
LPTOOLS=${OPX_LPTOOLS:-lptools}
METADATA=${OPX_METADATA:-/metadata}
TMP=${OPX_TMP:-/tmp}/opx-rom
LOG=${OPX_LOG:-${OPX_TMP:-/tmp}/opx-install.log}
MB=1048576
mkdir -p "$(dirname "$LOG")" 2>/dev/null
{ true > "$LOG"; } 2>/dev/null

note() { echo "$*" >> "$LOG" 2>/dev/null; }

ui() {
  note "$1"
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
  ui "! Details for support: $LOG (adb pull $LOG)"
  cleanup
  exit 1
}

prop() { "$GETPROP" "$1" 2>/dev/null; }

# the recovery's progress bar, 0..100
prog() {
  [ -n "$OUTFD" ] && [ -e "/proc/self/fd/$OUTFD" ] || return 0
  echo "set_progress $(($1 / 100)).$(printf '%02d' $(($1 % 100)))" > "/proc/self/fd/$OUTFD"
}

# Vol+ = yes, Vol- = no. No answer within KEY_WAIT seconds, or no way to read the keys: no.
ask() {
  ui "  Vol+ = YES     Vol- = NO     (no answer in $KEY_WAIT s = NO)"
  if ! command -v "$GETEVENT" >/dev/null 2>&1 || ! command -v "$TIMEOUT" >/dev/null 2>&1; then
    ui "  (this recovery can't read the volume keys: NO)"
    return 1
  fi
  end=$(( $(date +%s) + KEY_WAIT ))
  while [ "$(date +%s)" -lt "$end" ]; do
    left=$(( end - $(date +%s) ))
    [ "$left" -gt 0 ] || break
    ev=$("$TIMEOUT" "$left" "$GETEVENT" -lqc 1 2>/dev/null)
    case "$ev" in
      *KEY_VOLUMEUP*DOWN*) ui "  -> YES"; return 0 ;;
      *KEY_VOLUMEDOWN*DOWN*) ui "  -> NO"; return 1 ;;
    esac
  done
  ui "  -> no answer: NO"
  return 1
}

sha_of_entry() { unzip -p "$ZIP" "$1" 2>/dev/null | sha256sum | cut -d' ' -f1; }

# value of key=... in a build.prop
bprop() { sed -n "s/^$1=//p" "$TMP/installed.prop" | head -n 1; }

# Sizes are compared in 512-byte sectors and read in MiB: some recoveries' shells only count to
# 2^31 (2 GiB) and a partition like odm is bigger. The ROM list carries both, worked out on the PC.
devsectors() {
  if [ -f "$1" ]; then
    echo $(( ($(wc -c < "$1" | tr -d ' ') + 511) / 512 ))
    return
  fi
  b=$(basename "$(readlink -f "$1")")
  cat "/sys/class/block/$b/size" 2>/dev/null
}
blocks() { # name size -> "MiB sectors"
  got=$(sed -n "s/^blocks $1 //p" "$TMP/list" | head -n 1)
  [ -n "$got" ] && { echo "$got"; return; }
  echo "$((($2 + MB - 1) / MB)) $((($2 + 511) / 512))"  # zips made before 0.7.0-rc.53
}
mib_of() { blocks "$1" "$2" | cut -d' ' -f1; }
sectors_of() { blocks "$1" "$2" | cut -d' ' -f2; }

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
READY_SERIAL=$(sed -n 's/^ready serial //p' "$TMP/list" | head -n 1)
READY_MODEL=$(sed -n 's/^ready model //p' "$TMP/list" | head -n 1)
ui "ROM: $PRODUCT $BUILD"
if [ -n "$READY_SERIAL" ]; then
  have_serial=$(prop ro.serialno)
  [ -n "$have_serial" ] || have_serial=$(prop ro.boot.serialno)
  [ -n "$have_serial" ] || abort "Can't read this phone's serial number, so this convert ready zip can't confirm it's the phone it was made for."
  [ "$have_serial" = "$READY_SERIAL" ] || abort "This convert ready zip was made from another phone's backup ($READY_MODEL). It only installs on that phone."
  ui "- Convert ready: made for this phone ($READY_MODEL)"
fi

# --- 1. right phone ------------------------------------------------------------
# Device codenames only: ro.product.board is the chip platform (sun, canoe ...),
# shared by many phones, so it never counts.
ok=""; matched=""
for p in ro.product.device ro.build.product ro.product.vendor.device ro.product.odm.device; do
  have=$(prop "$p")
  [ -n "$have" ] || continue
  for b in $(sed -n 's/^board //p' "$TMP/list"); do
    [ "$have" = "$b" ] && { ok=1; matched=$b; }
  done
done
[ -n "$ok" ] || abort "This ROM is for $PRODUCT ($(sed -n 's/^board //p' "$TMP/list" | tr '\n' ' ')); this phone is '$(prop ro.product.device)'."

slot=$(prop ro.boot.slot_suffix)
case "$slot" in _a|_b) ;; *) abort "Can't tell which slot is active." ;; esac
ui "- Phone: $matched, slot ${slot#_}"
case "$slot" in _a) oslot=_b ;; *) oslot=_a ;; esac

# --- 2. nothing half-updated ---------------------------------------------------------
mount "$METADATA" >/dev/null 2>&1
if [ -d "$METADATA/ota/snapshots" ] && [ -n "$(ls -A "$METADATA/ota/snapshots" 2>/dev/null)" ]; then
  abort "A system update is still being applied (merge in progress). Boot Android, let it finish, then try again."
fi

# --- 3. what's installed: same variant, not newer than this ROM -------------------------
# OPPO keeps the phone's own model and build in my_manifest / odm / vendor; system
# often carries the chip platform's generic build, so all of them are read.
: > "$TMP/installed.prop"
if [ -n "$OPX_SYSTEM_PROP" ]; then
  cat "$OPX_SYSTEM_PROP" > "$TMP/installed.prop" 2>/dev/null
else
  mkdir -p "$TMP/sys"
  for part in my_manifest odm vendor product system_ext system; do
    for dev in "$MAPPER/$part$slot" "$BLOCK/$part$slot"; do
      [ -e "$dev" ] || continue
      mount -o ro "$dev" "$TMP/sys" >/dev/null 2>&1 \
        || mount -t erofs -o ro "$dev" "$TMP/sys" >/dev/null 2>&1 \
        || mount -t ext4 -o ro "$dev" "$TMP/sys" >/dev/null 2>&1 || continue
      for f in "$TMP/sys/build.prop" "$TMP/sys/etc/build.prop" "$TMP/sys/system/build.prop"; do
        [ -f "$f" ] && cat "$f" >> "$TMP/installed.prop"
      done
      umount "$TMP/sys" >/dev/null 2>&1
      break
    done
  done
fi
[ -s "$TMP/installed.prop" ] || abort "Can't read the installed Android's build, so a downgrade can't be ruled out. Flash from the PC with OPxFlasher instead."

generic() { case "$(echo "$1" | cut -d/ -f1 | tr 'A-Z' 'a-z')" in qti|qcom|android|generic|mediatek|mtk|unisoc|sprd|aosp|"") return 0 ;; esac; return 1; }

same_product=""; other=""; same_build=""
for fp in $(sed -n 's/^ro\.\([a-z_]*\.\)\{0,1\}build\.fingerprint=//p' "$TMP/installed.prop"); do
  generic "$fp" && continue
  prod=$(echo "$fp" | cut -d/ -f2)
  if [ "$prod" = "$PRODUCT" ]; then same_product=1; else other=$prod; fi
  [ -n "$ROM_FP" ] && [ "$fp" = "$ROM_FP" ] && same_build=1
done
for key in ro.product.model ro.product.vendor.model ro.product.odm.model ro.product.product.model; do
  [ "$(bprop "$key")" = "$PRODUCT" ] && same_product=1
done
REGION_CHANGE=""
if [ -n "$other" ] || [ -z "$same_product" ]; then
  if [ -z "$READY_SERIAL" ]; then
    [ -z "$other" ] || abort "This ROM is for $PRODUCT but the phone runs $other. Changing region needs a data wipe: use a convert ready zip (OPxFlasher > Convert, from your backup) or OPxFlasher on the PC."
    abort "Can't confirm the phone runs $PRODUCT (its build data doesn't say), so a region change can't be ruled out. Use OPxFlasher on the PC."
  fi
  REGION_CHANGE=1
  command -v "$TWRP" >/dev/null 2>&1 || abort "Changing region needs data formatted, and this recovery has no 'twrp' command to do it. Use OrangeFox or TWRP, or OPxFlasher on the PC."
  ui "- Region change: ${other:-?} -> $PRODUCT (data will be formatted)"
else
  ui "- Same variant: $PRODUCT"
fi

num() { echo "$1" | tr -cd '0-9'; }
# newest build date of any installed partition
HAVE_TS=$(sed -n 's/^ro\.\([a-z_]*\.\)\{0,1\}build\.date\.utc=//p' "$TMP/installed.prop" | tr -cd '0-9\n' | sort -n | tail -n 1)
HAVE_PATCH=$(sed -n 's/^ro\.\([a-z_]*\.\)\{0,1\}build\.version\.security_patch=//p' "$TMP/installed.prop" | tr -cd '0-9\n' | sort -n | tail -n 1)
newer=""
if [ -n "$same_build" ]; then
  newer=1  # reinstalling the build that's installed
elif [ -n "$ROM_TS" ] && [ -n "$HAVE_TS" ]; then
  [ "$(num "$ROM_TS")" -ge "$HAVE_TS" ] && newer=1
  [ -n "$newer" ] || abort "This ROM is OLDER than the installed Android. Installing older software can blow the anti-rollback fuse and brick the phone for good. Stopped."
elif [ -n "$ROM_PATCH" ] && [ -n "$HAVE_PATCH" ]; then
  [ "$(num "$ROM_PATCH")" -gt "$HAVE_PATCH" ] && newer=1
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
    have=$(devsectors "$dev")
    [ -n "$have" ] || abort "Can't read the size of $name on this phone."
    [ "$have" -ge "$(sectors_of "$name" "$size")" ] \
      || abort "$name ($size bytes) doesn't fit this phone's partition ($have sectors). Wrong ROM?"
    echo "physical $name $dev $size" >> "$TMP/targets"
    # firmware goes on both slots (as a fastboot flash does), so a fall-back to the other slot
    # doesn't start old firmware; the system partitions in super only exist for the active slot
    if [ "$dev" = "$BLOCK/$name$slot" ] && [ -e "$BLOCK/$name$oslot" ]; then
      have=$(devsectors "$BLOCK/$name$oslot")
      [ -n "$have" ] && [ "$have" -ge "$(sectors_of "$name" "$size")" ] \
        || abort "$name doesn't fit this phone's other slot. Wrong ROM?"
      echo "physical $name $BLOCK/$name$oslot $size" >> "$TMP/targets"
    fi
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
nparts=$(grep -c '^part ' "$TMP/list"); i=0
while read -r tag name index entry sha len; do
  [ "$tag" = part ] || continue
  [ "$(sha_of_entry "$entry")" = "$sha" ] || abort "$entry in the zip is damaged. Copy the zip to the phone again."
  i=$((i + 1)); prog $((i * 20 / nparts))
done < "$TMP/list"
ui "- Zip checked: all files intact"

# --- 6. convert ready: model change and network files are the user's call ------------------
: > "$TMP/targets_net"
for n in $(sed -n 's/^readyfile //p' "$TMP/list"); do
  grep " $n " "$TMP/targets" >> "$TMP/targets_net"
  grep -v " $n " "$TMP/targets" > "$TMP/targets.tmp"; mv "$TMP/targets.tmp" "$TMP/targets"
done
if [ -n "$REGION_CHANGE" ]; then
  ui " "
  ui "! MODEL CHANGE: this phone runs ${other:-another model}, this ROM is $PRODUCT."
  ui "! Only go on if this ROM is meant for your phone. Data will be formatted."
  ui "! You do this at your own risk: the OPxFlasher developer is not responsible"
  ui "! for any damage. Flash $PRODUCT $BUILD now?"
  ask || abort "Cancelled: you chose not to flash."
fi

cat "$TMP/targets" "$TMP/targets_net" > "$TMP/all_targets"

# --- 7. write ---
# A logical partition can only be resized and re-mapped once nothing uses it. Recoveries like
# AERA mount odm / vendor for their own services; lptools unmap then fails, the old mapping stays
# and the image would land where super no longer says the partition is. So: unmount and unmap
# everything first and stop BEFORE writing anything if a partition is still held.
MOUNTS=${OPX_MOUNTS:-/proc/mounts}
mounted_from() { # device -> its mount points
  [ -r "$MOUNTS" ] || return 0
  real=$(readlink -f "$1" 2>/dev/null)
  while read -r src mnt rest; do
    case "$src" in "$1"|"$real") echo "$mnt" ;; esac
  done < "$MOUNTS"
}
still_mapped() { [ -b "$1" ] || [ -e "$1.held" ]; }  # .held: the tests' stand-in for a busy device
release() { # name device
  for m in $(mounted_from "$2"); do umount "$m" >/dev/null 2>&1 || umount -l "$m" >/dev/null 2>&1; done
  out=$("$LPTOOLS" unmap "$1$slot" 2>&1); note "lptools unmap $1$slot: $? $out"
  still_mapped "$2" && return 1
  return 0
}
map_logical() { # kind name device size -> prints the block device to write to
  if out=$("$LPTOOLS" resize "$2$slot" "$4" 2>&1); then
    note "lptools resize $2$slot $4: ok $out"
  else
    note "lptools resize $2$slot $4: failed $out"
    [ "$1" = logical ] || return 1
    out=$("$LPTOOLS" create "$2$slot" "$4" 2>&1) || { note "lptools create: failed $out"; return 1; }
    note "lptools create $2$slot $4: ok $out"
  fi
  out=$("$LPTOOLS" map "$2$slot" 2>&1); note "lptools map $2$slot: $? $out"
  i=0
  while [ ! -e "$3" ] && [ "$i" -lt 10 ]; do sleep 1; i=$((i + 1)); done
  [ -e "$3" ] || return 1
  real=$(readlink -f "$3")
  b=$(basename "$real")
  if [ -d "/sys/class/block/$b/dm" ]; then  # a real dm device: the right one, at the new size
    dmname=$(cat "/sys/class/block/$b/dm/name" 2>/dev/null)
    have=$(devsectors "$real")
    want=$(sectors_of "$2" "$4")
    note "$2$slot -> $real ($dmname, $have sectors, image $want sectors)"
    [ "$dmname" = "$2$slot" ] || return 1
    [ -n "$have" ] && [ "$have" -ge "$want" ] && [ "$have" -lt $((want + 32768)) ] || return 1
  fi
  echo "$real"
}
check_one() { # name device size
  want=$(sed -n "s/^image [a-z]* $1 $3 //p" "$TMP/list" | head -n 1)
  got=$(dd if="$2" bs=$MB count="$(mib_of "$1" "$3")" 2>/dev/null | head -c "$3" | sha256sum | cut -d' ' -f1)
  note "read back $1 ($2): $got want $want"
  [ "$got" = "$want" ] && return 0
  explain "$1" "$2"
  return 1
}
# on a mismatch: which pieces differ, and what the kernel says the device is (for support)
explain() { # name device
  per=$((CHUNK / MB))
  grep "^part $1 " "$TMP/list" | while read -r tag name index entry sha len; do
    got=$(dd if="$2" bs=$MB skip=$((index * per)) count=$(((len + MB - 1) / MB)) 2>/dev/null | head -c "$len" | sha256sum | cut -d' ' -f1)
    [ "$got" = "$sha" ] && note "  piece $index ok" || note "  piece $index DIFFERS (at $((index * per)) MiB, $len bytes)"
  done
  real=$(readlink -f "$2"); b=$(basename "$real")
  note "  $2 -> $real, $(devsectors "$real") sectors, ro=$(cat "/sys/class/block/$b/ro" 2>/dev/null), dm=$(cat "/sys/class/block/$b/dm/name" 2>/dev/null)"
  note "  holders: $(ls "/sys/class/block/$b/holders" 2>/dev/null | tr '\n' ' ')"
  command -v dmctl >/dev/null 2>&1 && note "  $(dmctl table "$(cat "/sys/class/block/$b/dm/name" 2>/dev/null)" 2>&1 | tr '\n' ' ')"
  command -v dmsetup >/dev/null 2>&1 && note "  $(dmsetup table 2>&1 | tr '\n' ';')"
  grep -i " $1\|$real\|$2" "${OPX_MOUNTS:-/proc/mounts}" 2>/dev/null | while read -r ln; do note "  mounted: $ln"; done
}
for m in /system_root /system /vendor /product /odm /system_ext /vendor/odm /mnt/vendor/odm; do
  umount "$m" >/dev/null 2>&1
done
: > "$TMP/held"
while read -r kind name dev size; do
  [ "$kind" = physical ] && continue
  release "$name" "$dev" || echo "$name" >> "$TMP/held"
done < "$TMP/all_targets"
if [ -s "$TMP/held" ]; then
  abort "In use by this recovery, so it can't be resized safely: $(tr '\n' ' ' < "$TMP/held" | sed 's/ *$//'). Reboot to recovery (Reboot > Recovery) and flash again straight away. If it says this again, flash from the PC with OPxFlasher."
fi

write_parts() { # name device
  per=$((CHUNK / MB))
  grep "^part $1 " "$TMP/list" | while read -r tag name index entry sha len; do
    err=$(unzip -p "$ZIP" "$entry" | dd of="$2" bs=$MB seek=$((index * per)) conv=notrunc,fsync 2>&1) \
      || { note "write $1 piece $index failed: $err"; exit 1; }
  done
}

is_avb() { case "$1" in vbmeta|vbmeta_system|vbmeta_vendor) return 0 ;; esac; return 1; }

TOTAL=0
while read -r kind name dev size; do TOTAL=$((TOTAL + $(mib_of "$name" "$size"))); done < "$TMP/targets"
[ "$TOTAL" -gt 0 ] || TOTAL=1
WROTE=0
wrote() { WROTE=$((WROTE + $(mib_of "$1" "$2"))); prog $((20 + WROTE * 60 / TOTAL)); }
label() { case "$2" in *"$oslot") ;; *) ui "  $1" ;; esac; }  # one line per partition, not per slot

ui "- Writing firmware and boot images to both slots..."
while read -r kind name dev size; do
  [ "$kind" = physical ] || continue
  is_avb "$name" && continue
  label "$name" "$dev"
  write_parts "$name" "$dev" || fail_written "Writing $name failed."
  wrote "$name" "$size"
done < "$TMP/targets"

ui "- Writing system, vendor, odm, ... inside super..."
while read -r kind name dev size; do
  [ "$kind" = physical ] && continue
  ui "  $name"
  real=$(map_logical "$kind" "$name" "$dev" "$size") || fail_written "Can't make room for $name inside super."
  write_parts "$name" "$real" || fail_written "Writing $name failed."
  sync
  check_one "$name" "$real" "$size" || fail_written "$name doesn't match right after writing it."
  wrote "$name" "$size"
done < "$TMP/targets"

ui "- Writing vbmeta..."
while read -r kind name dev size; do
  [ "$kind" = physical ] && is_avb "$name" || continue
  label "$name" "$dev"
  write_parts "$name" "$dev" || fail_written "Writing $name failed."
  wrote "$name" "$size"
done < "$TMP/targets"
sync

# --- 8. read back --------------------------------------------------------------------
# everything again, from the disk rather than the cache: a later write must not have hit
# an earlier partition
ui "- Reading back what was written..."
echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
: > "$TMP/bad"
READ=0
while read -r kind name dev size; do
  check_one "$name" "$dev" "$size" || echo "${dev##*/}" >> "$TMP/bad"
  READ=$((READ + $(mib_of "$name" "$size"))); prog $((80 + READ * 20 / TOTAL))
done < "$TMP/targets"
[ -s "$TMP/bad" ] && fail_written "$(tr '\n' ' ' < "$TMP/bad" | sed 's/ *$//') doesn't match after writing."

# --- 9. your network files, if you want them -------------------------------------------------
net_done=""
if [ -s "$TMP/targets_net" ]; then
  ui " "
  ui "The ROM is installed. Flash your phone's own network files from your backup"
  ui "($(cut -d' ' -f2 "$TMP/targets_net" | tr '\n' ' ' | sed 's/ *$//'))? They keep mobile network working."
  if ask; then
    while read -r kind name dev size; do
      label "$name" "$dev"
      if [ "$kind" != physical ]; then
        dev=$(map_logical "$kind" "$name" "$dev" "$size") || fail_written "Can't make room for $name inside super."
      fi
      write_parts "$name" "$dev" || fail_written "Writing $name failed."
      sync
      check_one "$name" "$dev" "$size" || fail_written "$name doesn't match after writing."
    done < "$TMP/targets_net"
    sync
    net_done=1
  fi
fi

# --- 10. region change: data must go -------------------------------------------------------
formatted=""; on_data=""
for pre in ${OPX_INTERNAL:-/sdcard/ /data/ /storage/emulated/}; do
  case "$ZIP" in "$pre"*) on_data=1 ;; esac
done
if [ -n "$REGION_CHANGE" ] && [ -n "$on_data" ]; then
  : # internal storage lives inside data: data can't be formatted while this zip is read from it
elif [ -n "$REGION_CHANGE" ]; then
  ui "- Formatting data (needed for the new region)..."
  # time limited, and the exit code alone isn't trusted: a zip on internal storage (/sdcard is
  # inside /data) must be gone afterwards
  if command -v "$TIMEOUT" >/dev/null 2>&1; then
    "$TIMEOUT" 300 "$TWRP" format data >/dev/null 2>&1 && formatted=1
  else
    "$TWRP" format data >/dev/null 2>&1 && formatted=1
  fi
fi

ready=$(sed -n 's/^readyfile //p' "$TMP/list" | tr '\n' ' ' | sed 's/ *$//')
keeprec=$(grep -c '^keeprecovery$' "$TMP/list")
kept=$(sed -n 's/^kept //p' "$TMP/list" | tr '\n' ' ' | sed 's/ *$//')
written=$(sed -n 's/^netrom //p' "$TMP/list" | tr '\n' ' ' | sed 's/ *$//')
cleanup
ui " "
if [ -n "$REGION_CHANGE" ]; then
  ui "Done: $PRODUCT $BUILD is installed (region changed)."
else
  ui "Done: $PRODUCT $BUILD is installed. Your data was not touched."
fi
if [ -n "$ready" ] && [ -n "$net_done" ]; then
  ui "Mobile network: wrote your own $ready from your backup."
elif [ -n "$ready" ]; then
  ui "Mobile network: you chose not to write your $ready, so the phone keeps the"
  ui "ones it had. If calls or data don't work: OPxFlasher > Signal / region."
fi
if [ "$keeprec" -gt 0 ]; then
  ui "Recovery: left alone, your custom recovery stays."
fi
if [ -n "$REGION_CHANGE" ] && [ -n "$formatted" ]; then
  ui "Data was formatted. If the phone doesn't start, boot to recovery and"
  ui "format data again: Wipe > Format data > type yes."
fi
if [ -n "$REGION_CHANGE" ] && [ -z "$formatted" ] && [ -z "$on_data" ]; then
  ui " "
  ui "! Formatting data didn't work. BEFORE rebooting: Wipe > Format data > type yes."
  ui "! Without it the phone won't start in the new region."
  cleanup
  exit 1
fi
if [ -n "$kept" ]; then
  ui "Mobile network: kept your phone's own $kept."
fi
if [ -n "$written" ]; then
  ui "Mobile network: this ROM wrote its own $written (its vbmeta checks them at boot)."
  ui "If calls or mobile data don't work after booting, restore yours from your backup:"
  ui "OPxFlasher > Signal / region."
fi
prog 100
if [ -n "$REGION_CHANGE" ] && [ -z "$formatted" ]; then
  # the zip ran from internal storage, which is inside data
  ui " "
  ui "! ONE MORE STEP before rebooting: Wipe > Format data > type yes."
  ui "! (This zip was read from internal storage, which is part of data, so it"
  ui "! couldn't format data itself.) Without it the phone won't start in the new region."
  ui "! Then reboot to system. The first boot can take a few minutes."
  exit 0
fi
ui "Reboot to system. The first boot can take a few minutes."
exit 0
