"""Two PATCHes of different fields of one report must both land (no lost update)."""
import threading

from flask import Flask

from bug_report import create_blueprint

N = 60


def client(store):
    app = Flask(__name__)
    app.register_blueprint(create_blueprint(store=store, is_admin=lambda r: True,
                                            actor_identity=lambda r: {"email": "a@example.com", "name": ""}),
                           url_prefix="/api")
    return app.test_client()


def test_concurrent_patches_of_different_fields_all_land(any_store):
    c0 = client(any_store)
    bid = c0.post("/api/bugs", json={"details": "race me"}).get_json()["id"]
    problems, barrier = [], threading.Barrier(2)

    def worker(field, value_for):
        c = client(any_store)
        barrier.wait()
        for i in range(N):
            want = value_for(i)
            r = c.patch(f"/api/bugs/{bid}", json={field: want})
            if r.status_code != 200:
                problems.append((field, i, r.status_code, r.get_data(as_text=True)[:100]))
                continue
            # my write must still be there after the other thread's: a stale whole-record write puts the old value back
            got = c.get(f"/api/bugs/{bid}").get_json()[field]
            if got != want:
                problems.append((field, i, "lost", got, want))

    ts = [threading.Thread(target=worker, args=("area", lambda i: f"area-{i}")),
          threading.Thread(target=worker, args=("owner", lambda i: f"owner{i}@example.com"))]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert problems == []
    final = c0.get(f"/api/bugs/{bid}").get_json()
    assert (final["area"], final["owner"]) == (f"area-{N-1}", f"owner{N-1}@example.com")


def test_concurrent_votes_all_count(any_store):
    c0 = client(any_store)
    bid = c0.post("/api/bugs", json={"details": "vote race"}).get_json()["id"]
    barrier = threading.Barrier(8)

    def vote(n):
        barrier.wait()
        any_store.add_vote(bid, f"v{n}@example.com")

    ts = [threading.Thread(target=vote, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert any_store.vote_counts([bid])[bid] == 8
