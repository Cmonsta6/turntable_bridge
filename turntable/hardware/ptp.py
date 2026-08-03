"""
Raw PTP-over-USB transport and Nikon camera client.

THE CONTRACT EVERYTHING ELSE RELIES ON
--------------------------------------
A focus move is DeviceReady -> MfDrive(0x9204) -> check response ->
DeviceReady, and it ends in exactly one of:

    PTP_RC_OK                       the move completed
    NIKON_MfDriveStepEnd  (0xA00C)  the lens is against a mechanical stop
    NIKON_MfDriveStepInsufficiency  the step was too small to act on
    NIKON_NotLiveView     (0xA00B)  handled, never observed — read the next
                                    paragraph before believing anything about it

That fourth code was documented here as "LiveView is not running — explicit,
not silent", and this codebase was built on the belief that a focus move needs
a LiveView session: that MfDrive is inert without one, or refused outright.
MEASURED ON THE Z 6_2 ON 2026-08-03, no shutter actuations and no frames, and
the belief is FALSE for this body. With LiveView stopped (`stop_liveview`
called, `_liveview_on` False), `drive_to_stop(NEAR)` parked the lens after 600
units, `drive(+500)` was ACCEPTED and answered RC_OK (0x2001), and
`drive_to_stop(NEAR)` travelled 400 units recovering it — the lens really
moved. The same three commands as a control with LiveView UP came out
identical, 400 units back. An earlier one-shot `drive(+10)` with LiveView down
answered MfDriveStepEnd (0xA00C), i.e. already against the stop: accepted, not
refused. The 400-of-500 is `drive_to_stop`'s own chunked undercount — the chunk
that reports the stop has already arrived and is not counted, and
STOP_SWEEP_CHUNK is 200 — and it came out the same number both ways, which is
the whole point. LiveView made no difference to focus whatsoever.

So 0xA00B keeps its name in RC_NAMES and `NikonCamera.drive` still raises on
it, because the response code is real and handling it is right: another body,
or this one in some state nobody here has reached, may yet answer it. What is
wrong is only the CLAIM that a LiveView session is required.

CAPTURE was the half that trial deliberately did not touch, and this module
said so — "that needs a shutter actuation and has not been tested". It has
now been tested: 2026-08-03, same Z 6_2, three shutter actuations. Two
consecutive frames with LiveView DOWN both landed as real NEFs, 31.6 MB in
~1.1 s each; a control frame with LiveView UP came out 31.7 MB in 1.1 s.
Two in a row on purpose, because the InvalidObjectHandle failure this module
chased (see `delete_object`) surfaces on the frame AFTER the bad one, so a
single good frame proves nothing. DPC_NIKON_LIVEVIEW_PROHIBIT (0xD1A4) was
read after every frame and answered 0x00000000 throughout — bit 12, "pending
unretrieved SDRAM image", never set, so no buffer was left stuck either.

Capture is therefore as indifferent to LiveView as focus is: NOTHING THIS
MODULE DOES ON A RUN'S BEHALF NEEDS THE STREAM. Anything still saying capture
with LiveView down is untested or unsettled is stale — it was written between
the two trials.

One thing neither trial settles, and nothing here should be read as settling:
how any body other than the Z 6_2 behaves. One camera was measured.

An announced endstop plus a confirmed completion per move gives the same
guarantees absolute positioning would, which is why no position readback is
needed: an exact datum, and relative moves that cannot silently under-deliver.
Anything layered on top of this must propagate those codes rather than
collapsing them to "sent OK" — that distinction is the whole basis for the
app trusting its own step counter.

UNITS
-----
MfDrive's second parameter is an arbitrary uint32 of focus-driver units, so
this module works in DRIVER UNITS throughout and a gap of any size is one
command. The app's "step" is 10 driver units (`focus.STEP_UNITS`); the
conversion happens in `focus.drive_focus`, not here.

TRANSPORT
---------
Needs the camera bound to WinUSB (Zadig), not the Windows MTP driver. While
it is on WinUSB, Explorer cannot see the camera at all.
"""
from __future__ import annotations

import struct
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import usb.core
    import usb.util
except ImportError as _e:                                    # noqa: BLE001
    raise ImportError(
        "pyusb is required for the PTP transport: pip install pyusb "
        "libusb-package") from _e


NIKON_VID = 0x04B0

# ── Container types ──────────────────────────────────────────────────────
CONTAINER_COMMAND = 1
CONTAINER_DATA = 2
CONTAINER_RESPONSE = 3
CONTAINER_EVENT = 4

# ── Operations ───────────────────────────────────────────────────────────
OC_GET_DEVICE_INFO = 0x1001
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_GET_DEVICE_PROP_DESC = 0x1014
OC_GET_DEVICE_PROP_VALUE = 0x1015
OC_SET_DEVICE_PROP_VALUE = 0x1016

OC_GET_STORAGE_IDS = 0x1004
OC_GET_OBJECT_INFO = 0x1008
OC_GET_OBJECT = 0x1009
OC_DELETE_OBJECT = 0x100B
OC_INITIATE_CAPTURE = 0x100E

OC_NIKON_INITIATE_CAPTURE_REC_IN_SDRAM = 0x90C0
#: DO NOT USE. Kept named so nobody rediscovers it and assumes it is the right
#: way to free an SDRAM capture. It answers InvalidObjectHandle and corrupts
#: the camera's object bookkeeping — libgphoto2 commented out its own call to
#: this for the same reason (library.c:4451). Use standard DeleteObject.
OC_NIKON_DEL_IMAGE_SDRAM = 0x90C3
OC_NIKON_GET_EVENT = 0x90C7
OC_NIKON_DEVICE_READY = 0x90C8
OC_NIKON_GET_VENDOR_PROP_CODES = 0x90CA
OC_NIKON_START_LIVEVIEW = 0x9201
OC_NIKON_END_LIVEVIEW = 0x9202
OC_NIKON_GET_LIVEVIEW_IMG = 0x9203
OC_NIKON_MF_DRIVE = 0x9204
OC_NIKON_INITIATE_CAPTURE_REC_IN_MEDIA = 0x9207
OC_NIKON_TERMINATE_CAPTURE = 0x920C
OC_NIKON_GET_OBJECT_SIZE = 0x9421

# ── Events ───────────────────────────────────────────────────────────────
EC_OBJECT_ADDED = 0x4002
EC_DEVICE_PROP_CHANGED = 0x4006
EC_STORE_FULL = 0x400A
EC_CAPTURE_COMPLETE = 0x400D
EC_NIKON_OBJECT_ADDED_IN_SDRAM = 0xC101
EC_NIKON_CAPTURE_COMPLETE_REC_IN_SDRAM = 0xC102

#: Any of these means "a frame exists and here is its handle".
EC_OBJECT_READY = (EC_OBJECT_ADDED, EC_NIKON_OBJECT_ADDED_IN_SDRAM)

# ── Device properties ────────────────────────────────────────────────────
DPC_NIKON_RECORDING_MEDIA = 0xD10B      # card vs SDRAM

#: Values for RecordingMedia. Documented, not guessed: libgphoto2 ptp.c:8396-7
#: names 0 "Card" and 1 "SDRam". A Z6 II also accepts 2, which nobody has
#: publicly identified — so it is never written.
RECORDING_MEDIA_CARD = 0
RECORDING_MEDIA_SDRAM = 1
DPC_NIKON_LIVEVIEW_STATUS = 0xD1A2
DPC_NIKON_LIVEVIEW_PROHIBIT = 0xD1A4    # says WHY live view / focus is refused

# ── Response codes ───────────────────────────────────────────────────────
RC_OK = 0x2001
RC_SESSION_NOT_OPEN = 0x2003
RC_DEVICE_BUSY = 0x2019

RC_INVALID_OBJECT_HANDLE = 0x2009
RC_STORE_NOT_AVAILABLE = 0x2013

RC_NIKON_NOT_LIVEVIEW = 0xA00B
RC_NIKON_MFDRIVE_STEP_END = 0xA00C
RC_NIKON_MFDRIVE_INSUFFICIENT = 0xA00E

#: Response-code names, transcribed from the PTP spec via libgphoto2's ptp.h.
#:
#: Worth having RIGHT above all else. The abbreviated version this replaces
#: was missing 0x2009 and mislabelled 0x200A as AccessDenied (it is
#: DevicePropNotSupported; AccessDenied is 0x200F), so the single most
#: important error in this module's history — InvalidObjectHandle after a
#: capture — was reported to the user as "Unknown (0x2009)".
#:
#: NOT complete, though, and the same trap is still open six codes wide: the
#: standard range below skips 0x2011 SelfTestFailed, 0x2012 PartialDeletion,
#: 0x2014 SpecificationByFormatUnsupported, 0x2016 InvalidCodeFormat, 0x2017
#: UnknownVendorCode and 0x2020 SpecificationOfDestinationUnsupported — read
#: the jumps 0x2010 -> 0x2013 -> 0x2015 -> 0x2018 and the stop at 0x201F. Any
#: of those from the body prints through `rc_name` as exactly the "Unknown
#: (0x20xx)" this rewrite existed to eliminate, so add the entry rather than
#: chase the hex — the header this was transcribed from, libgphoto2's ptp.h,
#: carries the whole standard range to copy the name from.
RC_NAMES: Dict[int, str] = {
    0x2000: "Undefined",
    0x2001: "OK",
    0x2002: "GeneralError",
    0x2003: "SessionNotOpen",
    0x2004: "InvalidTransactionID",
    0x2005: "OperationNotSupported",
    0x2006: "ParameterNotSupported",
    0x2007: "IncompleteTransfer",
    0x2008: "InvalidStorageId",
    0x2009: "InvalidObjectHandle",
    0x200A: "DevicePropNotSupported",
    0x200B: "InvalidObjectFormatCode",
    0x200C: "StoreFull",
    0x200D: "ObjectWriteProtected",
    0x200E: "StoreReadOnly",
    0x200F: "AccessDenied",
    0x2010: "NoThumbnailPresent",
    0x2013: "StoreNotAvailable",
    0x2015: "NoValidObjectInfo",
    0x2018: "CaptureAlreadyTerminated",
    0x2019: "DeviceBusy",
    0x201A: "InvalidParentObject",
    0x201B: "InvalidDevicePropFormat",
    0x201C: "InvalidDevicePropValue",
    0x201D: "InvalidParameter",
    0x201E: "SessionAlreadyOpened",
    0x201F: "TransactionCanceled",
    0xA001: "NIKON_HardwareError",
    0xA002: "NIKON_OutOfFocus",
    0xA004: "NIKON_InvalidStatus",
    0xA005: "NIKON_SetPropertyNotSupported",
    0xA00B: "NIKON_NotLiveView",
    0xA00C: "NIKON_MfDriveStepEnd",
    0xA00E: "NIKON_MfDriveStepInsufficiency",
}

#: Driver units in one legacy "app step". Migration aid only — see UNITS above.
STEP_UNITS = 10

#: Direction signs. MfDrive flag 2 drives toward FAR/infinity, flag 1 toward
#: NEAR. The app uses positive-is-far throughout, so this is where the two
#: conventions meet.
FAR = +1
NEAR = -1

#: Units per command while sweeping to a mechanical stop.
#:
#: It is also the RESOLUTION of the distance such a sweep reports, and that
#: matters beyond homing: the chunk that finally answers MfDriveStepEnd
#: covered an unknown part of itself, so a sweep that returns N means the
#: lens was somewhere in [N, N + STOP_SWEEP_CHUNK). Anything comparing a
#: swept distance against a counter has to allow that whole bracket — see
#: `focus.anchored_drive`, which uses it to catch a counter that has gone
#: out of step with the hardware.
STOP_SWEEP_CHUNK = 200


def rc_name(code: int) -> str:
    return f"{RC_NAMES.get(code, 'Unknown')} (0x{code:04X})"


class PTPError(RuntimeError):
    """A transport-level failure, or an operation that returned non-OK."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class FocusAtLimit(PTPError):
    """The lens reached a mechanical stop. Not an error — a datum."""


# ═══════════════════════════════════════════════════════════════════════════
# Transport
# ═══════════════════════════════════════════════════════════════════════════
class PTPTransport:
    """
    PTP-over-USB framing.

    The protocol is small: every phase is one container with a 12-byte header
    (length, type, code, transaction id). A no-data operation is
    command -> response; a data-in operation is command -> data -> response.

    ONE TRANSACTION AT A TIME, ALWAYS. There is a single bulk pipe, and
    interleaving two transactions corrupts the stream for both — so a capture
    and a focus command cannot overlap. The lock around every exchange is not
    defensive, it is required.
    """

    READ_CHUNK = 512 * 128

    def __init__(self, timeout_ms: int = 8000):
        self._lock = threading.RLock()
        self._timeout = timeout_ms
        self._txn = 0
        self._buf = bytearray()
        self._pos = 0
        self._session_open = False
        self.dev = None
        self.intf = None
        self.ep_in = None
        self.ep_out = None

    # ── connection ───────────────────────────────────────────────────────
    def open(self) -> None:
        backend = _libusb_backend()
        dev = usb.core.find(idVendor=NIKON_VID, backend=backend)
        if dev is None:
            raise PTPError(
                "No Nikon USB device found. Check the camera is plugged in and "
                "switched on.\n" + ZADIG_HELP)
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass                        # already configured — normal

        cfg = dev.get_active_configuration()
        # Class 6 / subclass 1 / protocol 1 is "Still Image Capture, PTP".
        intf = usb.util.find_descriptor(cfg, bInterfaceClass=6)
        if intf is None:
            raise PTPError("No PTP (still image) interface on this device.")

        ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        if ep_in is None or ep_out is None:
            raise PTPError("Could not find the bulk IN/OUT endpoint pair.")

        # Claim eagerly so the "wrong driver" case surfaces here with an
        # actionable message, rather than as a traceback from inside the first
        # transfer several calls later.
        try:
            usb.util.claim_interface(dev, intf)
        except usb.core.USBError as e:
            # ERROR_ACCESS on claim does NOT mean the wrong driver here — it
            # means the interface is still claimed, almost always by a handle
            # this process failed to release after a timeout. A device reset
            # drops every outstanding claim, so try once before giving up;
            # otherwise a single wedged capture makes the camera unusable
            # until the app is restarted, which is exactly what happened.
            if e.errno not in (13, 5):                       # ACCESS, IO
                raise PTPError(f"Cannot claim the PTP interface ({e}).\n"
                               + ZADIG_HELP) from e
            try:
                dev.reset()
                usb.util.dispose_resources(dev)
                dev = usb.core.find(idVendor=NIKON_VID, backend=backend)
                if dev is None:
                    raise PTPError("The camera vanished during a USB reset.")
                dev.set_configuration()
                cfg = dev.get_active_configuration()
                intf = usb.util.find_descriptor(cfg, bInterfaceClass=6)
                ep_out = usb.util.find_descriptor(
                    intf, custom_match=lambda e2:
                    usb.util.endpoint_direction(e2.bEndpointAddress)
                    == usb.util.ENDPOINT_OUT
                    and usb.util.endpoint_type(e2.bmAttributes)
                    == usb.util.ENDPOINT_TYPE_BULK)
                ep_in = usb.util.find_descriptor(
                    intf, custom_match=lambda e2:
                    usb.util.endpoint_direction(e2.bEndpointAddress)
                    == usb.util.ENDPOINT_IN
                    and usb.util.endpoint_type(e2.bmAttributes)
                    == usb.util.ENDPOINT_TYPE_BULK)
                usb.util.claim_interface(dev, intf)
            except Exception as e2:                          # noqa: BLE001
                raise PTPError(
                    f"Cannot claim the PTP interface ({e}); a USB reset did "
                    f"not clear it either ({e2}). Unplug the camera and plug "
                    f"it back in.\n" + ZADIG_HELP) from e

        self.dev, self.intf, self.ep_in, self.ep_out = dev, intf, ep_in, ep_out
        self._txn = 0
        self._buf = bytearray()
        self._pos = 0

    #: Seconds allowed for the CloseSession courtesy call when shutting down.
    #: Deliberately tiny. close() is called BY THE RECOVERY PATH, so the device
    #: is usually already wedged — spending the full command timeout on a
    #: politeness that is about to be made irrelevant by releasing the
    #: interface cost 30 s per attempt and made recovery look like a hang.
    CLOSE_SESSION_TIMEOUT_MS = 1500

    def close(self) -> None:
        with self._lock:
            if self.dev is None:
                return
            # ALL FOUR handles are saved and restored together. Saving only
            # dev/intf left ep_out None, so the CloseSession below wrote to a
            # None endpoint, raised AttributeError, and was swallowed by the
            # bare except — the courtesy call was never sent on any disconnect,
            # and every session was left open on the body for open_session's
            # SessionAlreadyOpen path to clean up.
            dev, intf = self.dev, self.intf
            ep_in, ep_out = self.ep_in, self.ep_out
            # Cleared FIRST so nothing can start a new transaction against a
            # half-torn-down handle while we are inside this method.
            self.dev = self.intf = self.ep_in = self.ep_out = None
            try:
                if self._session_open:
                    # briefly, to talk
                    self.dev, self.intf = dev, intf
                    self.ep_in, self.ep_out = ep_in, ep_out
                    self._transact_locked(
                        OC_CLOSE_SESSION,
                        timeout_ms=self.CLOSE_SESSION_TIMEOUT_MS)
            except Exception:                                # noqa: BLE001
                pass
            finally:
                self.dev = self.intf = self.ep_in = self.ep_out = None
            self._session_open = False
            self._buf = bytearray()
            self._pos = 0
            # release THEN dispose, and never let either failure skip the
            # other — a retained claim is what makes the next open() fail.
            try:
                usb.util.release_interface(dev, intf)
            except Exception:                                # noqa: BLE001
                pass
            try:
                usb.util.dispose_resources(dev)
            except Exception:                                # noqa: BLE001
                pass

    def reset_stream(self) -> None:
        """
        Recover the pipe after a failed transaction.

        A timeout leaves TWO kinds of wreckage, and neither heals on its own:

        * Whatever bytes did arrive sit in the read buffer, usually a partial
          container. The next transaction reads that stale header, believes a
          length that was never real, and waits for bytes nobody will send —
          so one timeout becomes every subsequent call timing out. That is
          what turned a single hiccuped capture into an unrecoverable run.
        * The endpoint may be left stalled, which no amount of reading clears.

        So the buffer is dropped, the endpoints are un-stalled, and — the part
        that actually matters after a big transfer dies — the IN endpoint is
        DRAINED. Abandoning a 28 MB object read does not tell the camera to
        stop sending it: the rest keeps arriving, and the next transaction
        reads the tail of the dead transfer as though it were its own reply.
        Clearing our buffer alone therefore fixes nothing while data is still
        in flight.
        """
        with self._lock:
            self._buf = bytearray()
            self._pos = 0
            if self.dev is None:
                return
            for ep in (self.ep_in, self.ep_out):
                if ep is None:
                    continue
                try:
                    self.dev.clear_halt(ep.bEndpointAddress)
                except Exception:                            # noqa: BLE001
                    pass
            self.drain_input()

    #: How long to spend discarding in-flight data before giving up on it.
    DRAIN_BUDGET_S = 5.0

    def drain_input(self, budget_s: Optional[float] = None) -> int:
        """
        Read and discard until the IN endpoint runs dry. Returns bytes dropped.

        A read that times out is the SUCCESS condition here — it means nothing
        is left to arrive. Short per-read timeout so an already-empty pipe
        costs milliseconds rather than the whole budget.
        """
        if self.ep_in is None:
            return 0
        deadline = time.monotonic() + (budget_s or self.DRAIN_BUDGET_S)
        dropped = 0
        while time.monotonic() < deadline:
            try:
                chunk = self.ep_in.read(self.READ_CHUNK, 120)
            except Exception:                                # noqa: BLE001
                break                   # timed out = empty, which is the goal
            if not len(chunk):
                break
            dropped += len(chunk)
        return dropped

    def device_reset(self) -> None:
        """
        PTP class-specific Device Reset Request — the blunt instrument.

        Tells the camera to abandon whatever transaction it thinks is in
        flight and return to a clean idle state. The session goes with it, so
        the caller must open a new one.
        """
        with self._lock:
            if self.dev is None:
                return
            try:
                # 0x21 = host->device, class request, interface recipient.
                self.dev.ctrl_transfer(0x21, 0x66, 0,
                                       self.intf.bInterfaceNumber, None, 2000)
            except Exception:                                # noqa: BLE001
                pass
            self._session_open = False
            # The body needs a moment after a reset before it will answer
            # again; talking to it immediately just fails and looks like the
            # reset did not work.
            time.sleep(0.5)
            self.reset_stream()

    # ── framing ──────────────────────────────────────────────────────────
    #: Compact the read buffer once this many consumed bytes have piled up.
    COMPACT_AT = 1 << 20

    def _available(self) -> int:
        return len(self._buf) - self._pos

    def _fill(self, timeout_ms: int) -> None:
        chunk = self.ep_in.read(self.READ_CHUNK, timeout_ms)
        if not len(chunk):
            raise PTPError("empty bulk read — the camera stopped responding")
        self._buf.extend(chunk)

    def _read_container(self, timeout_ms: int) -> Tuple[int, int, bytes]:
        """
        Read one container, buffering any overshoot.

        A bulk read can in principle return more than the container it was
        after, so leftovers are kept rather than discarded. Dropping them would
        desynchronise the stream in a way that only shows up under load, which
        is precisely the class of bug this module exists to eliminate.

        The buffer is a bytearray consumed via a cursor rather than re-sliced
        per read. That is not tidiness: a 25 MB NEF arrives as ~400 chunks, and
        `buf = buf[length:]` on every one of them copies the whole remainder
        each time — quadratic, several GB of memcpy per frame, on a rig that
        shoots hundreds of frames per revolution.
        """
        while self._available() < 12:
            self._fill(timeout_ms)
        length, ctype, code, _txn = struct.unpack_from(
            "<IHHI", self._buf, self._pos)
        if length < 12:
            raise PTPError(f"nonsensical container length {length}")
        while self._available() < length:
            self._fill(timeout_ms)
        start = self._pos + 12
        payload = bytes(self._buf[start:self._pos + length])
        self._pos += length

        if self._pos == len(self._buf):          # fully drained — cheapest case
            self._buf.clear()
            self._pos = 0
        elif self._pos >= self.COMPACT_AT:
            del self._buf[:self._pos]
            self._pos = 0
        return ctype, code, payload

    def _transact_locked(self, code: int, params: Tuple[int, ...] = (),
                         data: Optional[bytes] = None,
                         timeout_ms: Optional[int] = None
                         ) -> Tuple[int, bytes, List[int]]:
        if self.dev is None:
            raise PTPError("transport is not open")
        t = timeout_ms or self._timeout
        try:
            return self._exchange(code, params, data, t)
        except Exception:
            # ANY failure leaves the pipe mid-container. Drop the wreckage
            # here, at the one place every transaction passes through, so the
            # NEXT call starts from a known-empty buffer instead of parsing
            # the tail of a transfer that died. Without this a single timeout
            # poisons every subsequent transaction and the run cannot recover.
            try:
                self.reset_stream()
            except Exception:                                # noqa: BLE001
                pass
            raise

    def _exchange(self, code: int, params: Tuple[int, ...],
                  data: Optional[bytes], timeout_ms: int
                  ) -> Tuple[int, bytes, List[int]]:
        self._txn += 1
        txn = self._txn
        body = b"".join(struct.pack("<I", p & 0xFFFFFFFF) for p in params)
        self.ep_out.write(
            struct.pack("<IHHI", 12 + len(body), CONTAINER_COMMAND, code, txn)
            + body, timeout_ms)

        if data is not None:
            self.ep_out.write(
                struct.pack("<IHHI", 12 + len(data), CONTAINER_DATA, code, txn)
                + data, timeout_ms)

        payload_in = b""
        ctype, rcode, payload = self._read_container(timeout_ms)
        if ctype == CONTAINER_DATA:
            payload_in = payload
            ctype, rcode, payload = self._read_container(timeout_ms)
        while ctype == CONTAINER_EVENT:          # async event; not our answer
            ctype, rcode, payload = self._read_container(timeout_ms)
        if ctype != CONTAINER_RESPONSE:
            raise PTPError(f"expected a response container, got type {ctype}")
        rparams = (list(struct.unpack_from("<" + "I" * (len(payload) // 4),
                                           payload, 0)) if payload else [])
        return rcode, payload_in, rparams

    def transact(self, code: int, params: Tuple[int, ...] = (),
                 data: Optional[bytes] = None,
                 timeout_ms: Optional[int] = None
                 ) -> Tuple[int, bytes, List[int]]:
        """
        Run one operation and hand back (response_code, data, response_params).

        Deliberately does NOT raise on a non-OK response: the response code is
        the signal this whole module exists to surface, so callers get to see
        it and decide. Use `check()` when a plain success is required.

        `timeout_ms` applies per bulk transfer, not to the whole operation, so
        it bounds how long the camera may go silent rather than how long an
        object may take to arrive. Capture passes a longer one, because a long
        exposure legitimately says nothing for the duration of the shutter.
        """
        if self.dev is None:
            raise PTPError("transport is not open")
        with self._lock:
            return self._transact_locked(code, params, data, timeout_ms)

    def check(self, code: int, params: Tuple[int, ...] = (),
              data: Optional[bytes] = None) -> bytes:
        """Run an operation and raise unless it answered OK."""
        rc, payload, _ = self.transact(code, params, data)
        if rc != RC_OK:
            raise PTPError(f"operation 0x{code:04X} failed: {rc_name(rc)}", rc)
        return payload

    # ── session ──────────────────────────────────────────────────────────
    def open_session(self, session_id: int = 1) -> None:
        rc, _, _ = self.transact(OC_OPEN_SESSION, (session_id,))
        # A session left open by a crashed process is reported as
        # SessionAlreadyOpen (0x201E); that is a usable session, not a failure.
        if rc not in (RC_OK, 0x201E):
            raise PTPError(f"OpenSession failed: {rc_name(rc)}", rc)
        self._session_open = True


def _libusb_backend():
    """
    pyusb needs libusb-1.0.dll, which Anaconda does not ship and Windows does
    not put on PATH. libusb-package bundles one, so prefer it.
    """
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:                                        # noqa: BLE001
        return None


ZADIG_HELP = """
The camera must be bound to WinUSB, not the Windows MTP driver:

  1. Run Zadig (https://zadig.akeo.ie) as administrator.
  2. Options -> List All Devices.
  3. Select 'Z 6_2' (USB ID 04B0:044C).
  4. Choose WinUSB, then Replace Driver.

To undo: Device Manager -> the camera -> Update driver -> Browse -> Let me
pick -> MTP USB Device.
"""


# ═══════════════════════════════════════════════════════════════════════════
# Nikon client
# ═══════════════════════════════════════════════════════════════════════════
class NikonCamera:
    """
    Focus and LiveView control for a Nikon body over raw PTP.

    Every method that moves the lens returns the camera's own verdict, so the
    caller never has to infer whether something happened.
    """

    #: Longest a single MfDrive is allowed to take before we call it wedged.
    #: A full-travel sweep on a Z6 II is ~1350 units; nothing legitimate should
    #: take anywhere near this, so it is a hang detector, not a pacing value.
    MOVE_TIMEOUT_S = 30.0

    #: How often to ask DeviceReady while a move is in flight. Cheap (one
    #: 12-byte transaction) and it only bounds the latency of NOTICING that the
    #: move finished, so it can be tight.
    READY_POLL_S = 0.02

    #: Per-transfer budget for the shutter command alone. Long, because a long
    #: exposure means the camera says nothing until it finishes.
    CAPTURE_TIMEOUT_MS = 90_000

    #: Per-transfer budget for fetching an object. Also long, and for a reason
    #: that cost a run to learn: the camera does real work before the FIRST
    #: byte of a 28 MB NEF appears — reading it out of the buffer and packing
    #: it. The transfer itself is quick once it starts (~1 s at USB 2 speeds,
    #: in 64 KB chunks), so this budget is almost entirely first-byte latency.
    #: A 6 s command timeout looked generous and timed out every capture.
    OBJECT_TIMEOUT_MS = 60_000

    #: How long the camera may stay busy AFTER the shutter command before we
    #: call it wedged. Covers the exposure plus committing the image.
    #: libgphoto2 allows 1000 s here (a D780 can expose for 900); this rig
    #: shoots stacks, not star trails, so a few minutes is ample and still
    #: bounded.
    CAPTURE_SETTLE_S = 180.0

    def recover(self) -> None:
        """
        Put the link back in a usable state after a failed operation.

        Escalates: drop any half-read container and un-stall the endpoints
        first, and only if the camera still will not answer, issue the PTP
        device reset and re-open the session. The cheap step fixes the common
        case (a timeout mid-transfer) without disturbing camera state.
        """
        self.t.reset_stream()
        try:
            self.wait_ready(timeout_s=3.0)
            return                                  # pipe is healthy again
        except Exception:                           # noqa: BLE001
            pass
        self.t.device_reset()
        self.t.open_session()
        self._liveview_on = False

    def __init__(self, transport: Optional[PTPTransport] = None):
        self.t = transport or PTPTransport()
        self.info: Dict = {}
        self._liveview_on = False
        # Latched once the camera says it cannot delete captured objects.
        self._delete_unsupported = False
        self._trace_fn: Optional[Callable[[str], None]] = None

    # ── lifecycle ────────────────────────────────────────────────────────
    #: Attempts allowed when first talking to the camera.
    #:
    #: A body that was power-cycled, woken, or left wedged by a previous
    #: session can be enumerable over USB while not yet answering PTP, so the
    #: first GetDeviceInfo times out and the second succeeds seconds later.
    #: Observed exactly that: a connect failing on the 6 s command timeout,
    #: then working on a manual retry. Making the user press Connect twice for
    #: something we can simply do ourselves is not a real requirement.
    CONNECT_ATTEMPTS = 3

    def connect(self) -> Dict:
        self.t.open()
        last: Optional[Exception] = None
        for attempt in range(self.CONNECT_ATTEMPTS):
            try:
                self.t.open_session()
                self.info = self.device_info()
                return self.info
            except Exception as e:                           # noqa: BLE001
                last = e
                self._trace(f"connect attempt {attempt + 1} failed ({e})")
                # Whatever half-answer arrived is wreckage; clear it before
                # asking again, or the retry parses the tail of the timeout.
                self.t.reset_stream()
                time.sleep(0.6)
        raise PTPError(
            f"the camera did not answer after {self.CONNECT_ATTEMPTS} "
            f"attempts: {last}")

    def close(self) -> None:
        try:
            if self._liveview_on:
                self.stop_liveview()
        except Exception:                                    # noqa: BLE001
            pass
        self.t.close()

    def set_trace(self, fn: Optional[Callable[[str], None]]) -> None:
        self._trace_fn = fn

    def _trace(self, msg: str) -> None:
        if self._trace_fn:
            try:
                self._trace_fn(msg)
            except Exception:                                # noqa: BLE001
                pass

    # ── identity ─────────────────────────────────────────────────────────
    def device_info(self) -> Dict:
        return parse_device_info(self.t.check(OC_GET_DEVICE_INFO))

    @property
    def model(self) -> str:
        return self.info.get("model", "unknown")

    def supports(self, opcode: int) -> bool:
        """
        Is `opcode` in the body's declared operation list?

        Treat a False as informational only. Z bodies have been reported to
        omit MfDrive from DeviceInfo while still honouring it, so nothing here
        gates a call on this — the response code is the real authority.
        """
        return opcode in self.info.get("operations", ())

    # ── live view ────────────────────────────────────────────────────────
    def start_liveview(self) -> None:
        """
        Start LiveView — the preview stream. NOT a precondition for focus.

        This said "Start LiveView, which MfDrive requires" for most of the life
        of this module, and it was the flattest statement of a belief that ran
        through the whole codebase. It was measured on the Z 6_2 on 2026-08-03
        and found false: MfDrive is accepted and moves the lens with LiveView
        stopped exactly as it does with LiveView running — the module docstring
        has the trial and the numbers. Nothing in this module gates focus on
        LiveView.

        One layer up it is a POLICY rather than a precondition, and both
        answers are deliberate. `PTPCameraClient._ensure_liveview` still starts
        the stream before a focus command by default, because outside a run
        reaching for a focus control is a prelude to looking through the lens
        and the user wants the preview back. A run does not want that, and
        `PTPCameraClient.hold_liveview_closed` turns it off:
        `PTPCameraClient._ensure_for_focus` then skips the restart and the
        stream stays down for the whole run. So expect to see focus moves with
        LiveView both up and down, and do not read a StartLiveView sitting next
        to an MfDrive as a requirement being satisfied — it is the preview
        being served, nothing more.

        What it IS for is `liveview_frame`, which returns nothing unless the
        body is streaming, and so everything built on that stream: the preview
        the UI shows through `PTPCameraClient.get_liveview_jpeg`, and the
        magnifier `PTPCameraClient.set_zoom` writes (0xD1BD LiveViewZoomArea —
        a LiveView property, with nothing to magnify while the stream is down).

        Idempotent-ish: a body that is already streaming answers InvalidStatus
        (0xA004), which is accepted here as success, so callers may call it
        unconditionally. The `wait_ready()` afterwards means the method does not
        return until the body has finished the state change, so the next
        transaction — whatever it is — is not issued into a camera still busy
        with it.

        Note what it does not do: this is a bare StartLiveView touching no
        device properties, so it does not carry libgphoto2's side effect of
        setting RecordingMedia. See `set_recording_media`.
        """
        rc, _, _ = self.t.transact(OC_NIKON_START_LIVEVIEW)
        if rc not in (RC_OK, 0xA004):        # A004 = InvalidStatus, i.e. already on
            raise PTPError(f"StartLiveView failed: {rc_name(rc)}", rc)
        self.wait_ready()
        self._liveview_on = True

    def stop_liveview(self) -> None:
        rc, _, _ = self.t.transact(OC_NIKON_END_LIVEVIEW)
        self._liveview_on = False
        if rc not in (RC_OK, 0xA004):
            raise PTPError(f"EndLiveView failed: {rc_name(rc)}", rc)
        self.wait_ready()

    def liveview_frame(self) -> Optional[bytes]:
        """
        One LiveView frame. The first 384 bytes are a header; the JPEG follows.
        Returns the whole payload, or None if LiveView is not running.
        """
        rc, data, _ = self.t.transact(OC_NIKON_GET_LIVEVIEW_IMG)
        if rc != RC_OK or len(data) <= 384:
            return None
        return data

    # ── DeviceReady ──────────────────────────────────────────────────────
    def wait_ready(self, timeout_s: Optional[float] = None,
                   poll_s: Optional[float] = None) -> int:
        """
        Poll DeviceReady until the camera stops answering DeviceBusy, and
        return the code it settled on.

        This loop is why nothing in this codebase sleeps a fixed interval to
        let the camera catch up: it asks rather than guesses.

        The settled code is meaningful, not just a gate: MfDriveStepEnd arrives
        here rather than from the MfDrive call itself, because the lens only
        discovers the stop while travelling.
        """
        limit = timeout_s or self.MOVE_TIMEOUT_S
        deadline = time.monotonic() + limit
        while True:
            rc, _, _ = self.t.transact(OC_NIKON_DEVICE_READY)
            if rc != RC_DEVICE_BUSY:
                return rc
            if time.monotonic() > deadline:
                raise PTPError(f"camera still busy after {limit:.0f}s", rc)
            time.sleep(poll_s or self.READY_POLL_S)

    # ── focus ────────────────────────────────────────────────────────────
    def drive(self, units: int, allow_limit: bool = True) -> int:
        """
        Move focus `units` driver units — positive toward FAR, negative NEAR —
        and do not return until the camera says the move is done.

        Returns the settled response code: RC_OK for a clean move, or
        RC_NIKON_MFDRIVE_STEP_END when the lens ran into a mechanical stop.
        With allow_limit=False the stop raises FocusAtLimit instead.

        There is no pacing delay between commands and none is needed: a
        transaction that has returned cannot collide with the next one. Do not
        add one.
        """
        if units == 0:
            return RC_OK
        flag = 2 if units > 0 else 1
        magnitude = abs(int(units))

        t0 = time.monotonic()
        # Never issue into a busy camera: MfDrive would be rejected and the
        # motion silently lost.
        self.wait_ready()
        rc, _, _ = self.t.transact(OC_NIKON_MF_DRIVE, (flag, magnitude))

        if rc == RC_NIKON_NOT_LIVEVIEW:
            # NEVER OBSERVED ON THE Z 6_2, and the reason this branch used to
            # give is wrong. It carried the claim that focus commands are inert
            # without a LiveView session; that was measured on 2026-08-03 and
            # found false — MfDrive is accepted and moves the lens with LiveView
            # down exactly as it does with LiveView up (numbers in the module
            # docstring). The branch stays, because the response code is real
            # and raising on it is right: some other body, or this one in a
            # state nobody here has reached, may still answer it, and a raise
            # beats reporting a move that did not happen as a move that did.
            # Only the explanation was ever wrong. If this does fire, 0xD1A4
            # (DPC_NIKON_LIVEVIEW_PROHIBIT) is the property to read — it is
            # documented as saying why the body is refusing.
            raise PTPError(
                "MfDrive was refused with NIKON_NotLiveView (0xA00B): the "
                "camera declined the command, so the lens did not move and no "
                "step counter should advance. What it is objecting to is not "
                "known — the name points at LiveView, but this body drives "
                "focus perfectly well with LiveView stopped, so do not assume "
                "starting LiveView is the fix.", rc)
        if rc == RC_NIKON_MFDRIVE_STEP_END:
            self._trace(f"MfDrive {units:+d} -> at limit on issue")
            if not allow_limit:
                raise FocusAtLimit("lens is against a mechanical stop", rc)
            return rc
        if rc != RC_OK:
            raise PTPError(f"MfDrive {units:+d} failed: {rc_name(rc)}", rc)

        settled = self.wait_ready()
        dt = (time.monotonic() - t0) * 1000.0
        self._trace(f"MfDrive {units:+d} -> {rc_name(settled)} [{dt:.0f} ms]")

        if settled == RC_NIKON_MFDRIVE_STEP_END:
            if not allow_limit:
                raise FocusAtLimit("lens reached a mechanical stop", settled)
            return settled
        if settled == RC_NIKON_MFDRIVE_INSUFFICIENT:
            return settled
        if settled != RC_OK:
            raise PTPError(f"MfDrive {units:+d} ended: {rc_name(settled)}",
                           settled)
        return RC_OK

    def at_limit(self, direction: int) -> bool:
        """Is the lens already against the stop in `direction`? One tiny probe."""
        rc = self.drive(1 if direction > 0 else -1)
        return rc == RC_NIKON_MFDRIVE_STEP_END

    def drive_to_stop(self, direction: int, chunk: int = STOP_SWEEP_CHUNK,
                      max_units: int = 4000,
                      check_stop: Optional[Callable[[], bool]] = None) -> int:
        """
        Drive to the mechanical stop in `direction` and return the units
        travelled getting there.

        This replaces HOME_SWEEP_STEPS = 800 and ANCHOR_MARGIN = 80 outright.
        Those existed because nothing could detect the stop, so homing drove
        further than the travel and trusted physics. The camera announces the
        stop with MfDriveStepEnd, so the sweep is exact and costs only as many
        commands as it takes to get there.

        The returned distance is a lower bound on how far the lens actually
        was from the stop: the final chunk ends early. For the travel figure
        use `measure_travel`, which brackets both stops.
        """
        travelled = 0
        step = chunk if direction > 0 else -chunk
        while travelled < max_units:
            if check_stop and check_stop():
                return travelled            # user abort, not a failure
            rc = self.drive(step)
            if rc == RC_NIKON_MFDRIVE_STEP_END:
                return travelled
            travelled += abs(step)
        raise PTPError(
            f"no MfDriveStepEnd after {travelled} units toward "
            f"{'FAR' if direction > 0 else 'NEAR'} — the stop was not found")

    def measure_travel(self, chunk: int = 100,
                       check_stop: Optional[Callable[[], bool]] = None) -> int:
        """
        Total focus travel in driver units, measured stop to stop and EXACT.

        Two passes, because a chunked walk alone cannot be exact. The chunk that
        finally reports MfDriveStepEnd has already arrived at the stop, so the
        distance it covered is unknown and unrecoverable — creeping onward from
        there measures nothing, since the lens is already there. The bracket
        that pass yields is `k*chunk <= travel < (k+1)*chunk`.

        So the second pass re-parks at NEAR, jumps the k*chunk that is now known
        to be safe in ONE command, and creeps the remainder in single units.
        That is what pins the figure. Costs one extra park plus at most `chunk`
        single-unit commands — a few seconds, no frames, no shutter actuations.

        Replaces focus_cal's frame-based bisection. The old method reported
        COMMANDED distance, so dropped commands made it systematically too large
        (it said 130 steps for a lens that has 115) — a one-directional bias.
        Nothing here is counted that the camera has not confirmed, so the figure
        cannot be inflated.
        """
        # Pass 1 — bracket the travel.
        self.drive_to_stop(NEAR, check_stop=check_stop)
        whole_chunks = 0
        while whole_chunks * chunk < 20_000:
            if check_stop and check_stop():
                return whole_chunks * chunk
            if self.drive(chunk) == RC_NIKON_MFDRIVE_STEP_END:
                break
            whole_chunks += 1
        else:
            raise PTPError("far stop not found while measuring travel")

        # Pass 2 — re-park and pin the remainder exactly.
        self.drive_to_stop(NEAR, check_stop=check_stop)
        base = whole_chunks * chunk
        # A cancel DURING the re-park makes drive_to_stop return early with the
        # lens still off the near stop. Falling through from there drives `base`
        # into the stop and reports "inconsistent travel" — a hardware fault
        # message for what was actually a button press. Bail on the same terms
        # as pass 1 instead: return what is known so far.
        if check_stop and check_stop():
            return base
        if base:
            if self.drive(base) == RC_NIKON_MFDRIVE_STEP_END:
                # The lens moved less this time than last, which means a stop
                # was reached early — the two passes disagree, so refuse to
                # report a number rather than report a wrong one.
                raise PTPError(
                    f"inconsistent travel: {base} units cleared on the first "
                    "pass but hit the stop on the second")
        # `remainder` counts steps that COMPLETED. The step that answers
        # MfDriveStepEnd is the one that reached the limit, so it counts too —
        # hence the +1. Getting this wrong costs exactly one driver unit, which
        # is 0.1 of an old app step and would never be noticed in use — which
        # is precisely why it was pinned by an offline test rather than left to
        # the eye. That test was a development script and is not shipped here.
        #
        # This does assume the body answers MfDriveStepEnd on ARRIVING at the
        # limit rather than only on being asked to go beyond it. If it turns out
        # to be the latter the figure is one unit high; at 1347 units of travel
        # that is 0.07% and affects nothing.
        remainder = 0
        while remainder <= chunk:
            if check_stop and check_stop():
                # RETURN, not break. A break here fell into the raise below, so
                # a cancel was reported to the user as "far stop not reached" —
                # focus_cal wraps CameraError as CalibrationError, so pressing
                # Cancel late in the wizard showed a hardware fault. The
                # documented contract (camera.measure_travel, focus_cal) is that
                # a cancel returns the distance covered so far.
                return base + remainder
            if self.drive(1) == RC_NIKON_MFDRIVE_STEP_END:
                return base + remainder + 1
            remainder += 1
        raise PTPError(
            f"far stop not reached within one chunk of {base} units")

    # ── device properties ────────────────────────────────────────────────
    def get_property(self, code: int) -> Optional[bytes]:
        rc, data, _ = self.t.transact(OC_GET_DEVICE_PROP_VALUE, (code,))
        return data if rc == RC_OK else None

    def set_property(self, code: int, data: bytes) -> int:
        rc, _, _ = self.t.transact(OC_SET_DEVICE_PROP_VALUE, (code,), data)
        return rc

    def vendor_property_codes(self) -> List[int]:
        """Nikon hides most of its properties behind this vendor call."""
        rc, data, _ = self.t.transact(OC_NIKON_GET_VENDOR_PROP_CODES)
        if rc != RC_OK or not data:
            return []
        return read_u16_array(data, 0)[0]

    def prop_desc(self, code: int) -> Optional[Dict]:
        """
        Full description of a property: type, writability, and the values it
        will actually accept.

        Worth having rather than guessing. RecordingMedia's encoding of "card"
        versus "SDRAM" is not documented anywhere trustworthy, and writing the
        wrong number to it decides where every frame of a run ends up.
        """
        rc, data, _ = self.t.transact(OC_GET_DEVICE_PROP_DESC, (code,))
        if rc != RC_OK or not data:
            return None
        return parse_device_prop_desc(data)

    def get_prop(self, code: int) -> Optional[int]:
        """Read a property as an integer, using its declared type."""
        desc = self.prop_desc(code)
        raw = self.get_property(code)
        if desc is None or raw is None:
            return None
        try:
            return read_typed(raw, 0, desc["data_type"])[0]
        except Exception:                                    # noqa: BLE001
            return None

    def set_prop(self, code: int, value: int) -> None:
        """Write an integer property, packed to its declared type."""
        desc = self.prop_desc(code)
        if desc is None:
            raise PTPError(f"property 0x{code:04X} is not described by the camera")
        allowed = desc.get("enum")
        if allowed and value not in allowed:
            raise PTPError(
                f"property 0x{code:04X} will not accept {value}; "
                f"it allows {allowed}")
        rc, _, _ = self.t.transact(OC_SET_DEVICE_PROP_VALUE, (code,),
                                   pack_typed(value, desc["data_type"]))
        if rc != RC_OK:
            raise PTPError(f"setting 0x{code:04X}={value} failed: {rc_name(rc)}",
                           rc)

    # ── events ───────────────────────────────────────────────────────────
    def get_events(self) -> List[Tuple[int, int]]:
        """
        Drain the camera's event queue as [(code, param), ...].

        Nikon's payload is a uint16 count followed by 6 bytes per event: a
        uint16 code and a uint32 parameter. For an ObjectAdded the parameter is
        the object handle, which is the only way to find out what was just shot.
        """
        rc, data, _ = self.t.transact(OC_NIKON_GET_EVENT)
        if rc != RC_OK or len(data) < 2:
            return []
        (count,) = struct.unpack_from("<H", data, 0)
        events: List[Tuple[int, int]] = []
        for i in range(count):
            off = 2 + 6 * i
            if off + 6 > len(data):
                break
            events.append(tuple(struct.unpack_from("<HI", data, off)))
        return events

    def wait_for_object(self, timeout_s: float = 30.0,
                        poll_s: float = 0.05) -> int:
        """
        Block until the camera reports a new object and return its handle.

        Raises on timeout rather than returning a sentinel, because a caller
        that ignores this has already tripped the shutter — silence here would
        lose a frame that physically exists.
        """
        for handle in self._object_handles(timeout_s, poll_s):
            return handle
        raise PTPError(
            f"no object appeared within {timeout_s:.0f}s of the shutter firing")

    def _object_handles(self, timeout_s: float, poll_s: float = 0.05):
        """
        Yield every new object handle the camera reports, until `timeout_s`.

        A generator rather than a single value because ONE SHUTTER RELEASE CAN
        PRODUCE MORE THAN ONE OBJECT EVENT, and not all of them are readable.
        Observed on a Z6 II: an SDRAM capture also raises a plain ObjectAdded
        carrying a card-style handle (0x0B000001) that GetObjectInfo rejects
        with InvalidObjectHandle. Committing to the first event seen therefore
        failed the capture — and the caller, reasonably believing the shutter
        had not fired, fired it AGAIN. That cost a real actuation per hiccup.

        So candidates are offered one at a time and the caller keeps asking
        until one of them actually reads.
        """
        deadline = time.monotonic() + timeout_s
        seen = set()
        while True:
            for code, param in self.get_events():
                if code == EC_STORE_FULL:
                    raise PTPError("the card is full")
                if code in EC_OBJECT_READY and param not in seen:
                    seen.add(param)
                    self._trace(f"object event 0x{code:04X} -> handle "
                                f"0x{param:08X}")
                    yield param
            if time.monotonic() > deadline:
                return
            time.sleep(poll_s)

    # ── capture ──────────────────────────────────────────────────────────
    def capture(self, to_sdram: bool) -> None:
        """
        Trip the shutter and do not return until the camera is idle again.

        Not the handshake — the whole frame, and anything reasoning about run
        timing has to budget for it. In order: a `wait_ready()` before firing
        (MOVE_TIMEOUT_S, 30 s); the shutter transaction itself, which may
        legitimately say nothing for the length of a long exposure and so gets
        CAPTURE_TIMEOUT_MS (90 s) per transfer, re-sent for up to 10 s if the
        body answers DeviceBusy to the trigger (a refusal, not a fire — see
        the loop below); then the DeviceReady poll at the bottom of this
        method, which spans the exposure and the image commit and is bounded
        by CAPTURE_SETTLE_S (180 s). What is NOT waited for here is the
        object — the handle is announced and the bytes read afterwards, by
        `shoot`.

        NEVER RETRIED, at any layer. A capture that errors or times out has
        usually already fired — re-sending it double-exposes the subject and
        burns an actuation. Everything else in this module is safe to re-issue;
        this one is not, and that asymmetry is deliberate.

        LIVEVIEW IS IRRELEVANT HERE, both ways round, and that was measured on
        2026-08-03 rather than assumed — two consecutive frames with the stream
        down came out as real 31.6 MB NEFs matching a LiveView-up control, with
        0xD1A4 clean after every one (numbers in the module docstring). So
        there is no need to toggle it around a capture in either direction, and
        a run that holds LiveView closed throughout is not doing anything this
        method minds. Toggling would also cost a state change the next focus
        command has to wait out.
        """
        self.set_recording_media(to_sdram)
        self.wait_ready()
        # A long exposure legitimately keeps the camera silent for its whole
        # duration, so this one operation gets a much longer per-transfer
        # budget than an ordinary command. Everything else stays short, so a
        # wedged link is noticed in seconds rather than tying up recovery.
        op, params = (
            # 0xFFFFFFFF = no AF area, i.e. do not refocus before firing.
            (OC_NIKON_INITIATE_CAPTURE_REC_IN_SDRAM, (0xFFFFFFFF,)) if to_sdram
            else (OC_NIKON_INITIATE_CAPTURE_REC_IN_MEDIA, (0xFFFFFFFF, 0x0000)))

        # DeviceBusy means the trigger was REFUSED, so the shutter did not
        # fire and re-sending is safe — the one case where retrying a capture
        # is correct. libgphoto2 loops here for the same reason.
        deadline = time.monotonic() + 10.0
        while True:
            rc, _, _ = self.t.transact(op, params,
                                       timeout_ms=self.CAPTURE_TIMEOUT_MS)
            if rc != RC_DEVICE_BUSY:
                break
            if time.monotonic() > deadline:
                raise PTPError("the camera stayed busy and never accepted the "
                               "shutter command", rc)
            time.sleep(0.05)
        if rc != RC_OK:
            raise PTPError(f"capture failed: {rc_name(rc)}", rc)

        # THE GATE THIS WAS MISSING, and the cause of the capture failures.
        #
        # The camera reports BUSY for the whole exposure and while it commits
        # the image — libgphoto2 waits here with a 1000 s budget, commenting
        # "busyness will be reported during the whole of the exposure time".
        # We went straight to polling for the object event instead, which
        # announces a handle BEFORE the object is readable. Fetching in that
        # window is what produced both symptoms seen on a real run:
        # GetObjectInfo answering InvalidObjectHandle (0x2009), and the object
        # read stalling long enough to blow any sane timeout.
        #
        # Polled coarsely: this is seconds of legitimate work, not a race, and
        # a 20 ms poll would spend thousands of transactions saying "still no".
        self.wait_ready(timeout_s=self.CAPTURE_SETTLE_S, poll_s=0.1)

    def set_recording_media(self, to_sdram: bool) -> None:
        """
        Point the camera at the buffer or the card, to match the capture.

        THIS IS NOT OPTIONAL, and omitting it was the cause of the capture
        failures. `InitiateCaptureRecInSdram` against a body whose
        RecordingMedia says "Card" is incoherent: the shutter fires, the
        camera raises ObjectAddedInSDRAM, and there is no SDRAM object behind
        the handle — so GetObjectInfo answers InvalidObjectHandle. The
        diagnostic caught it reading 0 (Card) while every capture asked for
        SDRAM. libgphoto2 sets this to SDRam as part of enabling LiveView
        (library.c:3778-3782); our `start_liveview` is a bare
        OC_NIKON_START_LIVEVIEW transaction that touches no properties at
        all, so we never inherited that side effect and had to do it
        explicitly.

        The reason recorded here has now been wrong twice, in opposite
        directions, which is worth keeping as a warning about how easy this
        paragraph is to get wrong. First it read "we close LiveView for runs",
        which was not true. Then it read the reverse — that the run's own first
        focus move re-opens LiveView and the hide cannot be made to hold —
        which was accurate only while every focus command went through
        `PTPCameraClient._ensure_liveview`, and is stale now that
        `PTPCameraClient.hold_liveview_closed` keeps the stream down for a
        whole run. Whether LiveView is up has no bearing on this property, so
        both versions were beside the point regardless; what matters is only
        that OUR enable path does not carry libgphoto2's.

        Written only when it differs, and kept in step with the capture mode
        both ways — so a keep-on-camera capture puts it back to Card by
        itself, rather than leaving the body in a state the user did not ask
        for.
        """
        want = RECORDING_MEDIA_SDRAM if to_sdram else RECORDING_MEDIA_CARD
        desc = self.prop_desc(DPC_NIKON_RECORDING_MEDIA)
        if desc is None:
            return                      # body does not expose it; nothing to do
        if desc.get("current") == want:
            return
        if not desc.get("writable"):
            raise PTPError(
                f"the camera records to "
                f"{'card' if desc.get('current') == RECORDING_MEDIA_CARD else 'SDRAM'}"
                " and will not let us change it, so this capture cannot be "
                "delivered the way it was asked for")
        allowed = desc.get("enum")
        if allowed and want not in allowed:
            raise PTPError(f"the camera will not accept RecordingMedia={want}; "
                           f"it allows {allowed}")
        self.set_prop(DPC_NIKON_RECORDING_MEDIA, want)
        self._trace(f"RecordingMedia -> {'SDRam' if to_sdram else 'Card'}")

    def object_info(self, handle: int) -> Dict:
        rc, data, _ = self.t.transact(OC_GET_OBJECT_INFO, (handle,),
                                      timeout_ms=self.OBJECT_TIMEOUT_MS)
        if rc != RC_OK:
            raise PTPError(f"GetObjectInfo(0x{handle:08X}) failed: "
                           f"{rc_name(rc)}", rc)
        return parse_object_info(data)

    def read_object(self, handle: int) -> bytes:
        rc, data, _ = self.t.transact(OC_GET_OBJECT, (handle,),
                                      timeout_ms=self.OBJECT_TIMEOUT_MS)
        if rc != RC_OK:
            raise PTPError(f"GetObject(0x{handle:08X}) failed: {rc_name(rc)}",
                           rc)
        return data

    def delete_object(self, handle: int) -> bool:
        """
        Free a captured object. Returns False if this camera cannot, which is
        not an error — the buffer is reused by the next capture regardless.

        USES STANDARD DeleteObject, NOT `DelImageSDRAM` (0x90C3). That matters
        more than it looks: DelImageSDRAM is what this code used to call after
        every frame, and it is precisely what libgphoto2 COMMENTED OUT, with
        the note (library.c:4451):

            this does result in 0x2009 (invalid object handle) with the D90

        which is the exact failure seen here — captures succeeding for a few
        frames and then one coming back InvalidObjectHandle. Swallowing the
        delete's own error hid it: the damage showed up on the NEXT capture,
        whose object the camera then would not hand over.

        The latch mirrors libgphoto2's `deletesdramfails`. Once a camera has
        said it cannot delete, asking again every frame is just repeatedly
        poking a body that already answered.
        """
        if self._delete_unsupported:
            return False
        rc, _, _ = self.t.transact(OC_DELETE_OBJECT, (handle, 0))
        if rc == RC_OK:
            return True
        if rc in (RC_INVALID_OBJECT_HANDLE, RC_STORE_NOT_AVAILABLE):
            self._delete_unsupported = True
            self._trace(f"this camera will not delete captured objects "
                        f"({rc_name(rc)}); not asking again")
            return False
        self._trace(f"deleting 0x{handle:08X} failed: {rc_name(rc)}")
        return False

    def shoot(self, dest_dir: Optional[Path] = None,
              filename: Optional[str] = None,
              keep_on_camera: bool = False,
              timeout_s: float = 30.0,
              overwrite: bool = False) -> Optional[Path]:
        """
        Fire one frame; return the path written, or None when keeping it on the
        card.

        `keep_on_camera` writes to the card and pulls nothing over USB — fast,
        but the app then has no NEF to verify focus against.

        Otherwise the frame goes to the camera's SDRAM and is pulled straight
        off, so no card is needed at all. That distinction was a real bug in the
        old bridge: "not on the card" was implemented as save-to-PC-AND-card, so
        shooting with no card in the body failed every capture.

        PASS `filename`. A frame captured to SDRAM never gets a card filename,
        so the body reports a PLACEHOLDER — every shot comes back as
        DSC_0000.NEF. Measured, not guessed: five frames of visibly different
        sizes all reported that one name, and four silently overwrote each
        other. So the CALLER owns naming; do not trust the reported name.

        A frame is never lost regardless: if the target exists and `overwrite`
        is False, a numeric suffix is added and the ACTUAL path returned.
        Refusing to write would throw away an image that has already cost a
        shutter actuation, which is the one thing not worth protecting.
        """
        self.get_events()                       # discard anything stale first
        self.capture(to_sdram=not keep_on_camera)
        if keep_on_camera:
            self.wait_for_object(timeout_s)     # confirm it actually landed
            return None

        # Try each object the camera announces until one actually reads. An
        # unreadable handle is NOT a failed capture: the shutter has already
        # fired, so giving up here makes the caller fire it again.
        handle = info = payload = None
        rejected = []
        for candidate in self._object_handles(timeout_s):
            try:
                info = self.object_info(candidate)
                payload = self.read_object(candidate)
                handle = candidate
                break
            except PTPError as e:
                why = rc_name(e.code) if e.code is not None else str(e)
                rejected.append(f"0x{candidate:08X} ({why})")
                self._trace(f"handle 0x{candidate:08X} did not read ({why}); "
                            "waiting for another")
        if handle is None:
            raise PTPError(
                "the shutter fired but no readable object arrived"
                + (f" — rejected {', '.join(rejected)}" if rejected else ""))

        self.delete_object(handle)      # best effort; reused by the next shot

        if dest_dir is None:
            return None
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        reported = info.get("filename") or f"object_{handle:08X}.nef"
        if filename:
            # Keep the body's extension unless the caller supplied one, so a
            # caller naming frames "pos001_0007" still gets a .NEF.
            name = filename if Path(filename).suffix else \
                filename + Path(reported).suffix
        else:
            name = reported
        path = dest_dir / name
        if not overwrite:
            path = _unused_path(path)
        path.write_bytes(payload)
        return path


# ═══════════════════════════════════════════════════════════════════════════
# PTP dataset parsing
# ═══════════════════════════════════════════════════════════════════════════
def read_ptp_string(buf: bytes, off: int) -> Tuple[str, int]:
    n = buf[off]
    off += 1
    if n == 0:
        return "", off
    raw = buf[off: off + n * 2]
    return raw.decode("utf-16-le", errors="replace").rstrip("\x00"), off + n * 2


def read_u16_array(buf: bytes, off: int) -> Tuple[List[int], int]:
    (count,) = struct.unpack_from("<I", buf, off)
    off += 4
    return list(struct.unpack_from("<" + "H" * count, buf, off)), off + count * 2


#: PTP data-type code -> (struct format, byte width).
DTC_FORMATS: Dict[int, Tuple[str, int]] = {
    0x0001: ("<b", 1), 0x0002: ("<B", 1),
    0x0003: ("<h", 2), 0x0004: ("<H", 2),
    0x0005: ("<i", 4), 0x0006: ("<I", 4),
    0x0007: ("<q", 8), 0x0008: ("<Q", 8),
}
DTC_ARRAY_MASK = 0x4000
DTC_STR = 0xFFFF


def read_typed(buf: bytes, off: int, dtc: int):
    """Read one PTP-typed value; returns (value, offset_after)."""
    if dtc == DTC_STR:
        return read_ptp_string(buf, off)
    if dtc & DTC_ARRAY_MASK:
        base = dtc & ~DTC_ARRAY_MASK
        (count,) = struct.unpack_from("<I", buf, off)
        off += 4
        values = []
        for _ in range(count):
            value, off = read_typed(buf, off, base)
            values.append(value)
        return values, off
    fmt = DTC_FORMATS.get(dtc)
    if fmt is None:
        raise PTPError(f"unsupported PTP data type 0x{dtc:04X}")
    return struct.unpack_from(fmt[0], buf, off)[0], off + fmt[1]


def pack_typed(value: int, dtc: int) -> bytes:
    fmt = DTC_FORMATS.get(dtc)
    if fmt is None:
        raise PTPError(f"cannot pack PTP data type 0x{dtc:04X}")
    return struct.pack(fmt[0], value)


def parse_device_prop_desc(buf: bytes) -> Dict:
    """
    Parse a GetDevicePropDesc dataset.

    The form field at the end is the useful part: it says whether a property
    takes a RANGE (min/max/step) or a fixed ENUM of values. That turns "set
    RecordingMedia to 1 and hope" into "the camera accepts exactly these".
    """
    code, data_type, get_set = struct.unpack_from("<HHB", buf, 0)
    off = 5
    default, off = read_typed(buf, off, data_type)
    current, off = read_typed(buf, off, data_type)
    desc: Dict = {
        "code": code,
        "data_type": data_type,
        "writable": get_set == 1,
        "default": default,
        "current": current,
        "range": None,
        "enum": None,
    }
    if off >= len(buf):
        return desc
    form = buf[off]
    off += 1
    if form == 1:                                   # range
        lo, off = read_typed(buf, off, data_type)
        hi, off = read_typed(buf, off, data_type)
        step, off = read_typed(buf, off, data_type)
        desc["range"] = (lo, hi, step)
    elif form == 2:                                 # enumeration
        (count,) = struct.unpack_from("<H", buf, off)
        off += 2
        values = []
        for _ in range(count):
            value, off = read_typed(buf, off, data_type)
            values.append(value)
        desc["enum"] = values
    return desc


#: Field offsets in a PTP ObjectInfo dataset. Taken from libgphoto2's
#: ptp-pack.c rather than derived, because the filename is the one field this
#: rig actually needs and an off-by-one there yields plausible garbage: the
#: fixed header is 52 bytes, the filename LENGTH byte sits at 52, and its
#: UTF-16 characters start at 53.
OI_STORAGE_ID = 0
OI_OBJECT_FORMAT = 4
OI_OBJECT_SIZE = 8
OI_PARENT_OBJECT = 38
OI_FILENAME_LEN = 52


def _unused_path(path: Path) -> Path:
    """
    The given path, or the first free `name_0001` variant of it.

    Exists so a captured frame can never be silently destroyed by the next one.
    """
    if not path.exists():
        return path
    for n in range(1, 10000):
        candidate = path.with_name(f"{path.stem}_{n:04d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise PTPError(f"cannot find a free filename near {path}")


def parse_object_info(buf: bytes) -> Dict:
    if len(buf) < OI_FILENAME_LEN + 5:
        raise PTPError(f"ObjectInfo too short: {len(buf)} bytes")
    storage_id = struct.unpack_from("<I", buf, OI_STORAGE_ID)[0]
    object_format = struct.unpack_from("<H", buf, OI_OBJECT_FORMAT)[0]
    size = struct.unpack_from("<I", buf, OI_OBJECT_SIZE)[0]

    # Some devices emit a 64-bit ObjectCompressedSize, which shifts everything
    # after it by four bytes. libgphoto2 detects it exactly this way. A Nikon
    # should never do it, but the check is three lines and the failure mode it
    # prevents is a garbled filename rather than an obvious error.
    shift = 4 if (buf[OI_FILENAME_LEN] == 0
                  and len(buf) > OI_FILENAME_LEN + 4
                  and buf[OI_FILENAME_LEN + 4] != 0) else 0
    filename, _ = read_ptp_string(buf, OI_FILENAME_LEN + shift)
    return {
        "storage_id": storage_id,
        "object_format": object_format,
        "size": size,
        "parent": struct.unpack_from("<I", buf, OI_PARENT_OBJECT + shift)[0],
        "filename": filename,
    }


def parse_device_info(buf: bytes) -> Dict:
    off = 0
    std_ver, vendor_ext, vendor_ver = struct.unpack_from("<HIH", buf, off)
    off += 8
    _desc, off = read_ptp_string(buf, off)
    off += 2                                    # FunctionalMode
    operations, off = read_u16_array(buf, off)
    events, off = read_u16_array(buf, off)
    properties, off = read_u16_array(buf, off)
    _capture_formats, off = read_u16_array(buf, off)
    _image_formats, off = read_u16_array(buf, off)
    manufacturer, off = read_ptp_string(buf, off)
    model, off = read_ptp_string(buf, off)
    device_version, off = read_ptp_string(buf, off)
    serial, off = read_ptp_string(buf, off)
    return {
        "standard_version": std_ver,
        # Some Z bodies report 0xFFFFFFFF here instead of the Nikon id, so
        # nothing in this module gates a Nikon opcode on it.
        "vendor_extension_id": vendor_ext,
        "vendor_extension_version": vendor_ver,
        "operations": operations,
        "events": events,
        "properties": properties,
        "manufacturer": manufacturer,
        "model": model,
        "device_version": device_version,
        "serial": serial,
    }
