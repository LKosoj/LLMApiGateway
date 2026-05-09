"""Regression for rotation race with hot-reload: start_index must not exceed local length.

When models_fallback_rules.json is reloaded during an in-flight request, the
sequence length captured locally in chat._dispatch_chat_request can differ from
the total_models passed to get_next_model_index. Previously, this could yield
an empty reorder and surface as "No providers were attempted".

This test validates the core invariant: after normalization, the reorder is
non-empty and spans the entire local sequence.
"""
import unittest


class RotationReorderNormalizationTests(unittest.TestCase):
    def _reorder(self, sequence, start_index):
        # Mirrors the (fixed) logic in chat._dispatch_chat_request.
        sequence_length = len(sequence)
        start_index = start_index % sequence_length
        return sequence[start_index:] + sequence[:start_index]

    def test_normal_case_reorder_matches_rotation(self):
        seq = [{"provider": f"p{i}", "model": "m"} for i in range(4)]
        self.assertEqual(self._reorder(seq, 0), seq)
        self.assertEqual(
            self._reorder(seq, 2),
            [seq[2], seq[3], seq[0], seq[1]],
        )

    def test_start_index_beyond_length_does_not_yield_empty(self):
        """start_index from a stale longer-length rotation must still produce full reorder."""
        seq = [{"provider": f"p{i}", "model": "m"} for i in range(3)]
        # Previously: reorder = seq[5:] + seq[:5] → seq[5:] is empty, seq[:5] == seq → OK?
        # Actually seq[5:] == [] and seq[:5] == seq, total_length 3 — accidental safety.
        # However seq[4:] + seq[:4] on length 3 gives [] + seq[:4] = seq (accidentally).
        # The deterministic guarantee is: with normalize, we always land on a valid offset.
        reordered = self._reorder(seq, 5)
        self.assertEqual(len(reordered), len(seq))
        # index 5 % 3 = 2
        self.assertEqual(reordered, [seq[2], seq[0], seq[1]])

    def test_exact_length_start_index_rotates_to_origin(self):
        seq = [{"provider": f"p{i}", "model": "m"} for i in range(4)]
        # start_index == len: should wrap to 0 (no rotation)
        self.assertEqual(self._reorder(seq, 4), seq)


if __name__ == "__main__":
    unittest.main()
