"""Generated Analyzer recorder: stereo plus a recorded counter and host-beat witness."""

from live_maestro.audio.capture_protocol import (
    CAPTURE_SCHEMA,
    OPEN_ADDRESS,
    PATH_ADDRESS,
    REPORT_ADDRESS,
    START_ADDRESS,
    STOP_ADDRESS,
)
from live_maestro.maxgen import Patch


def add_capture(patch: Patch, plugin: str, receiver: str, track_id: str) -> None:
    patch.comment("Capture records L/R, DSP counter, host beat, transport and validity flags. "
                  "The worker exports stereo and reports the observed host timing resolution.")
    router = patch.obj(f"route {OPEN_ADDRESS} {START_ADDRESS} {STOP_ADDRESS}")
    patch.link(receiver, 0, router, 0)
    recorder = patch.obj("sfrecord~ 6")
    patch.link(plugin, 0, recorder, 0)
    patch.link(plugin, 1, recorder, 1)
    counter = patch.obj("count~")
    patch.link(counter, 0, recorder, 2)
    sync = patch.obj("plugsync~")
    beat_signal = patch.obj("sig~")
    patch.link(sync, 6, beat_signal, 0)
    patch.link(beat_signal, 0, recorder, 3)
    playing_signal = patch.obj("sig~")
    flags_signal = patch.obj("sig~")
    patch.link(sync, 0, playing_signal, 0)
    patch.link(sync, 8, flags_signal, 0)
    patch.link(playing_signal, 0, recorder, 4)
    patch.link(flags_signal, 0, recorder, 5)
    snapshot = patch.obj("snapshot~")
    patch.link(counter, 0, snapshot, 0)
    pack = patch.obj("pack 0 0 0 0 0 0. 0. 0", inlets=8)
    patch.link(snapshot, 0, pack, 5)
    patch.link(sync, 6, pack, 6)
    patch.link(sync, 8, pack, 7)
    patch.link(track_id, 1, pack, 4)
    rate = patch.obj("dspstate~")
    patch.link(rate, 1, pack, 3)
    address = patch.obj(f"prepend {REPORT_ADDRESS}")
    sender = patch.obj("udpsend 127.0.0.1 9883")
    patch.link(pack, 0, address, 0)
    patch.link(address, 0, sender, 0)
    alive = patch.obj("loadbang")
    floating = patch.message("samptype float32")
    patch.link(alive, 0, floating, 0)
    patch.link(floating, 0, recorder, 0)
    path = patch.obj("zl reg")

    for outlet, phase in ((0, 0), (1, 1), (2, 2)):
        sequence = patch.obj("t l l")
        patch.link(router, outlet, sequence, 0)
        if phase == 0:
            split = patch.obj("zl slice 3")
            symbol = patch.obj("tosymbol")
            patch.link(sequence, 1, split, 0)
            patch.link(split, 1, symbol, 0)
            patch.link(symbol, 0, path, 1)
        fields = patch.obj("unpack 0 0 0")
        patch.link(sequence, 0, fields, 0)
        aim = patch.obj("prepend port")
        patch.link(fields, 2, aim, 0)
        patch.link(aim, 0, sender, 0)
        patch.link(fields, 1, pack, 1)
        match = patch.obj("== 0")
        patch.link(track_id, 1, match, 1)
        patch.link(fields, 0, match, 0)
        accepted = patch.obj("select 1")
        patch.link(match, 0, accepted, 0)
        run = patch.obj("t b b")
        patch.link(accepted, 0, run, 0)
        if phase == 0:
            opened = patch.obj("prepend open")
            prepare = patch.obj("t b b")
            patch.link(run, 1, prepare, 0)
            patch.link(prepare, 1, counter, 0)
            patch.link(prepare, 0, path, 0)
            patch.link(path, 0, opened, 0)
            patch.link(opened, 0, recorder, 0)
            echo = patch.obj(f"prepend {PATH_ADDRESS}")
            patch.link(path, 0, echo, 0)
            patch.link(echo, 0, sender, 0)
        elif phase == 1:
            message = patch.message("1")
            patch.link(run, 1, message, 0)
            patch.link(message, 0, recorder, 0)
        else:
            message = patch.message("0")
            patch.link(run, 1, message, 0)
            patch.link(message, 0, recorder, 0)
        reply = patch.obj("t b b b b")
        patch.link(run, 0, reply, 0)
        phase_message = patch.message(str(phase))
        patch.link(reply, 3, phase_message, 0)
        patch.link(phase_message, 0, pack, 2)
        patch.link(reply, 2, rate, 0)
        patch.link(reply, 1, snapshot, 0)
        schema = patch.message(str(CAPTURE_SCHEMA))
        patch.link(reply, 0, schema, 0)
        patch.link(schema, 0, pack, 0)
