
---

# Part 1: EA-only test — no Python required

## 1. Stop Python completely

On the VPS:

- Stop the bot/server.
- Close any Python terminal.
- Do not leave the UI bot running.
- Make sure there are no existing positions or pending orders using magic `123456`.

In MT5, check:

- **Trade → Positions**
- **Trade → Orders**

Close/remove any old test orders before continuing.

---

## 2. Copy and compile the updated EA

Copy the updated source file:

`WTExecutor.mq5`

to the MT5 data folder:

```text
File → Open Data Folder
MQL5 → Experts
```

Replace the old `WTExecutor.mq5`.

Open MetaEditor with `F4`, open `WTExecutor.mq5`, and press `F7`.

You need:

```text
0 errors
```

Warnings should be investigated, but compilation must have zero errors.

The updated source contains:

```mql5
#define ARM_SLOTS 4096
```

This is required for the 500-limit + 500-market test.

---

## 3. Attach the EA

In MT5:

1. Open the `FX Vol 20` chart.
2. Open Navigator with `Ctrl+N`.
3. Find `WTExecutor`.
4. Drag it onto the chart.
5. On the **Common** tab, enable:
   - Allow Algo Trading
6. On the **Inputs** tab, use:

```text
InpPollMs              = 5
InpReplyWaitMs         = 8000
InpSweepMagic          = 123456
InpPendingFilling     = ORDER_FILLING_RETURN
TestCancelDelayMs      = 0
TestUnfillableWinner  = false
```

The test inputs must remain disabled for the normal happy-path test.

Enable Algo Trading globally. The toolbar button must be green.

In the MT5 **Experts** log, confirm something similar to:

```text
WT executor v1.3 ready
```

Also note the printed Common Files directory. It will look similar to:

```text
C:\Users\<WindowsUser>\AppData\Roaming\MetaQuotes\Terminal\Common\Files
```

That is where you will write `wt_cmd.txt`.

---

## 4. Create a small 2 + 2 command first

Do not start with 500 + 500.

First test:

- 2 buy limits
- 2 sell limits
- 2 contingent market sells
- 2 contingent market buys

You need two levels that bracket the current price.

For example, if the current price is approximately `1000`:

```text
lower=990
upper=1010
```

Use prices valid for the actual symbol digits and broker stops level.

Create a file named `wt_cmd.txt` in the Common Files folder:

```text
id=low2
action=ARMLIMIT
symbol=FX Vol 20
magic=123456
lower=990.00000
upper=1010.00000
armed_timeout_ms=60000
win_fill_deadline_ms=5000
cancel_ack_deadline_ms=5000
burst_mode=AFTER_CANCEL
PB|1|0.01|0.00|0.00|low2PB001
PB|2|0.01|0.00|0.00|low2PB002
PS|1|0.01|0.00|0.00|low2PS001
PS|2|0.01|0.00|0.00|low2PS002
CS|1|0.01|0.00|0.00|low2CS001
CS|2|0.01|0.00|0.00|low2CS002
CB|1|0.01|0.00|0.00|low2CB001
CB|2|0.01|0.00|0.00|low2CB002
```

Important:

- `PB` = buy-limit ladder at `lower`.
- `PS` = sell-limit ladder at `upper`.
- `CS` = contingent market sells if the lower buy ladder wins.
- `CB` = contingent market buys if the upper sell ladder wins.

The EA stores both possible scenarios up front, but it only fires the appropriate contingent side after the trigger.

### Safer PowerShell method

Use a temporary file and rename it, so the EA does not read a partially written command:

```powershell
$common = "$env:APPDATA\MetaQuotes\Terminal\Common\Files"

@"
id=low2
action=ARMLIMIT
symbol=FX Vol 20
magic=123456
lower=990.00000
upper=1010.00000
armed_timeout_ms=60000
win_fill_deadline_ms=5000
cancel_ack_deadline_ms=5000
burst_mode=AFTER_CANCEL
PB|1|0.01|0.00|0.00|low2PB001
PB|2|0.01|0.00|0.00|low2PB002
PS|1|0.01|0.00|0.00|low2PS001
PS|2|0.01|0.00|0.00|low2PS002
CS|1|0.01|0.00|0.00|low2CS001
CS|2|0.01|0.00|0.00|low2CS002
CB|1|0.01|0.00|0.00|low2CB001
CB|2|0.01|0.00|0.00|low2CB002
"@ | Set-Content -Path "$common\wt_cmd.tmp" -Encoding ascii

Move-Item -Force "$common\wt_cmd.tmp" "$common\wt_cmd.txt"
```

The EA should delete `wt_cmd.txt` after reading it.

---

## 5. Confirm the arm was accepted

Within a few milliseconds, inspect:

```text
wt_res.txt
```

You should see something similar to:

```text
accepted=1
reason=ARMING
```

Then inspect:

```text
wt_arm_low2.txt
```

You should see:

```text
phase=ARMED
```

In MT5's Trade tab, confirm:

- 2 buy limits at the lower price;
- 2 sell limits at the upper price;
- no market positions yet.

If the phase becomes `ABORT`, inspect the `reason`.

Common causes:

- `PLACE_SHORT`
- invalid price distance;
- invalid filling mode;
- wrong symbol;
- insufficient margin;
- broker pending-order restriction.

---

## 6. Trigger the lower buy limits

Let the market fall to the lower level.

When the buy limits trigger, watch the MT5 **Experts** log and `wt_arm_low2.txt`.

Expected sequence:

```text
phase=TRIGGERED
```

Then:

1. The EA detects the buy-limit winner.
2. It submits cancellation requests for the sell limits.
3. The sell limits disappear from MT5's Orders tab.
4. The winning buy limits finish filling.
5. The EA submits the `CS` market sells asynchronously.
6. The phase becomes:

```text
phase=DONE
```

For a lower buy-limit trigger, you should see:

```text
trigger_side=1
```

The final positions should be:

```text
2 buy positions from PB
2 sell positions from CS
```

There should be:

```text
0 pending orders
```

The `CB` lines must not be opened in this scenario.

### What timing to record

The phase file includes timestamps such as:

```text
t_trigger_us=
t_cancel_us=
t_burst_us=
```

Calculate:

```text
cancel delay = t_cancel_us - t_trigger_us
burst delay  = t_burst_us - t_trigger_us
```

The MT5 Experts log also shows the event sequence.

With `AFTER_CANCEL`, the sell burst starts only after:

- the winning buy ladder has completed;
- the losing sell-limit cancellations have resolved.

---

## 7. Manually close the test positions

Because this is an EA-only test, Python will not reconcile or manage the positions.

After verifying the result, manually close all test positions.

Do not leave test positions open before the next test.

---

# Part 2: Test the upper sell-limit trigger

Repeat the same test with a new command ID, for example:

```text
id=up2
```

Let price rise to the upper level.

Expected result:

```text
2 sell positions from PS
2 buy positions from CB
0 pending orders
```

The phase should report:

```text
trigger_side=2
```

The losing `PB` buy limits should disappear.

The `CS` market sells must not be opened for this scenario.

This test also confirms the corrected upper-trigger logging.

---

# Part 3: Test timeout cleanup

Send an arm command with a wide level and:

```text
armed_timeout_ms=10000
```

Do not allow price to reach either level.

Expected result after approximately 10 seconds:

```text
phase=ABORT
reason=ARM_TIMEOUT
```

Then confirm in MT5:

```text
0 pending orders
```

Also confirm the EA log contains the machine-release message.

---

# Part 4: Test the 500 + 500 configuration without Python

Once 2 + 2 works, test a smaller scale:

```text
10 + 10
50 + 50
100 + 100
500 + 500
```

For the final 500 + 500 arm:

- `PB`: 500 buy limits
- `PS`: 500 sell limits
- `CS`: 500 contingent market sells
- `CB`: 500 contingent market buys

Total internal EA lines:

```text
500 + 500 + 500 + 500 = 2,000
```

The updated EA capacity is:

```text
ARM_SLOTS = 4096
```

so the source-side storage is sufficient.

However, before sending the 500 + 500 command, verify:

1. The broker/account allows at least 1,000 pending orders.
2. The terminal can display and manage that many orders.
3. Margin is sufficient.
4. The broker allows the total volume.
5. There are no old orders using magic `123456`.

At `0.01` lots:

```text
500 buys = 5.00 lots
500 sells = 5.00 lots
```

But you still need the broker to allow approximately 1,000 pending orders simultaneously.

For the large test, start with:

```text
armed_timeout_ms=120000
win_fill_deadline_ms=10000
cancel_ack_deadline_ms=10000
burst_mode=AFTER_CANCEL
```

The original 1.5-second deadlines may be too aggressive for 500 fills/cancellations.

---