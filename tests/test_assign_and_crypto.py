import base64

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from schulblick.sph.cryptor import Cryptor, decrypt_with, encrypt_with
from schulblick.sph.parsers import CalendarEvent, MessageHeader
from schulblick.sync.assign import ChildProfile, assign_event, assign_message, assign_substitution, parse_audience

KIDS = [
    ChildProfile("1", "Max", "Muster, Max", "G8a", {"französisch g8a", "religion - katholisch 8"}),
    ChildProfile("2", "Paul", "Muster, Paul Peter", "G5b", {"englisch g5b", "religion - katholisch 5"},
                 lessons={(3, 8): {"LZ56 M", "LZR56 D"}}),
    ChildProfile("3", "Lena", "Muster, Lena Marie", "G5c", {"ethik 5", "biologie g5c"}),
]


def ev(title, desc="", category="8", group="", tool=""):
    return CalendarEvent("x", title, "2026-11-01", None, True, category, "", desc, group, tool)


def test_exam_by_course_group():
    kind, ids, _ = assign_event(ev("Religion - katholisch 5 Arbeit", group="Religion - katholisch 5 (051RKA01-GYM)",
                                   tool="Klausurplaner"), KIDS, ("15",), ())
    assert kind == "exam" and ids == ["2"]
    kind, ids, _ = assign_event(ev("Ethik 5 (g5a, g5c) Lernkontrolle", group="Ethik 5 (g5a, g5c) (051ETHI02-GYM)",
                                   tool="Klausurplaner"), KIDS, ("15",), ())
    assert kind == "test" and ids == ["3"]


def test_general_event_audiences():
    assert assign_event(ev("Zahnarzt JG5-7"), KIDS, (), ())[1] == ["2", "3"]
    assert assign_event(ev("G9+R9: Berufspraktikum"), KIDS, (), ())[1] == []
    assert assign_event(ev("Weihnachtskonzert"), KIDS, (), ())[1] == ["*"]
    assert assign_event(ev("R10/H9 Fahrt nach Hadamar"), KIDS, (), ())[1] == []
    _, ids, parents_only = assign_event(ev("Ski-Infoelternabend", "Eltern der Jahrgangsstufe 8"), KIDS, (),
                                        ("Elternabend",))
    assert ids == ["1"] and parents_only
    assert assign_event(ev("Schulkonferenz", category="15"), KIDS, ("15",), ())[1] == []
    assert assign_event(ev("Fachtag", "Es findet kein Unterricht statt.", category="15"), KIDS, ("15",), ())[0] == "holiday"
    assert parse_audience("Raum R201") == []


def test_substitution_certainty():
    row = {"hours": "8", "classes": "G5a, G5b, G5c", "subject": "LZR56 D", "kind": "Entfall"}
    result = assign_substitution(row, 3, KIDS)  # Thursday
    assert result == {"2": "sure", "3": "maybe"}
    assert assign_substitution({"hours": "1 - 2", "classes": "G8c", "subject": "E"}, 4, KIDS) == {}


def msg(groups=(), recipients=()):
    return MessageHeader("m", "s", "x", "heute 09:00", True, list(groups), list(recipients), False)


def test_message_visibility():
    assert assign_message(msg(["Eltern der Lerngruppe Biologie g5c (051BIO06-GYM)"]), KIDS) == (["3"], [])
    assert assign_message(msg(["Alle Lehrkräfte", "Alle Lernenden (SuS)"]), KIDS) == (["*"], ["*"])
    assert assign_message(msg(["Eltern der Klasse G8a", "Lernende der Klasse G8a"]), KIDS) == (["1"], ["1"])
    assert assign_message(msg(["Alle Eltern"]), KIDS) == (["*"], [])
    assert assign_message(msg([], ["Muster, Anna", "Muster, Thomas"]), KIDS) == ([], [])
    assert assign_message(msg([], ["Muster, Paul Peter"]), KIDS) == (["2"], ["2"])


def test_cryptor_handshake_roundtrip():
    server_key = RSA.generate(2048)
    client = Cryptor()
    wrapped = client.wrapped_key(server_key.publickey().export_key().decode())
    # server side: unwrap the key and answer with the key encrypted under itself
    secret = PKCS1_v1_5.new(server_key).decrypt(base64.b64decode(wrapped), None)
    assert secret == client.key
    assert client.verify_challenge(encrypt_with(secret, secret))
    assert client.decrypt(encrypt_with(secret, '[{"Betreff": "Hallo"}]'.encode())) == '[{"Betreff": "Hallo"}]'
    assert decrypt_with(b"k", encrypt_with(b"k", b"x" * 33)) == b"x" * 33
    assert not Cryptor().verify_challenge(encrypt_with(b"other", b"other"))
