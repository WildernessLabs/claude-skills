# ---------------------------------------------------------------------------
# Batch GDB session template.  Copy, fill in the address block, run with:
#
#   perl -e 'alarm 540; exec @ARGV' <gdb> -q -batch -x session.gdb fw.elf \
#        > out.log 2>&1
#
# then filter:
#   perl -pe 's/\e\[[0-9;]*[a-zA-Z]//g' out.log | grep -E "^(STAGE|RET|STOP|DONE)"
#
# ADDRESSES CHANGE ON EVERY BUILD.  Re-derive before each session.
# ---------------------------------------------------------------------------

set confirm off
set pagination off
set height 0
set width 0

# --- address block: EDIT ---------------------------------------------------
set $STAGE1     = 0x00000000   # flash addr reached AFTER any bootloader RAM copy
set $TARGET     = 0x00000000   # instruction of interest
set $RAM_LO     = 0x00000000   # RAM exec region, for classifying frames
set $RAM_HI     = 0x00000000

define connect
  target extended-remote localhost:3333
  # add-symbol-file second.elf 0xADDR    # multi-image builds
end

# Two-stage arming.  Nothing may be armed before this: a reset clears hardware
# triggers, and a bootloader that reloads RAM from flash erases software
# breakpoints placed there.  GDB reports both as armed regardless.
define stage1
  monitor reset halt
  delete
  eval "hbreak *%d", $STAGE1
  continue
  printf "STAGE1 pc=%#x\n", $pc
end

# Guarded frame dump.  Never read frame-relative memory without checking $pc:
# a stop somewhere unexpected yields plausible-looking garbage.
define frm
  if ($pc != $TARGET && $pc != $TARGET + 4)
    printf "WRONG STOP pc=%#010x -- values meaningless\n", $pc
  else
    printf "STOP pc=%#010x sp=%#010x\n", $pc, $sp
    # add target-specific field decoding here
  end
end

# Bounded hunt.  Always cap the loop and provide an early exit; an unbounded
# loop against a stalled target runs until the alarm kills it.
define hunt
  set $n = 0
  set $done = 0
  while ($n < 80 && $done == 0)
    continue
    if ($pc == $TARGET)
      printf "RET %3d sp=%#010x\n", $n, $sp
      # set $done = 1 when the interesting condition is seen
    else
      printf "RET %3d WRONG STOP pc=%#010x\n", $n, $pc
    end
    set $n = $n + 1
  end
  printf "DONE done=%d stops=%d\n", $done, $n
end

# Two samples seconds apart: identical state means stuck, differing means
# running.  Cheapest first move on any "it hangs" report.
define where
  monitor halt
  printf "PC=%#x\n", $pc
  bt
end
