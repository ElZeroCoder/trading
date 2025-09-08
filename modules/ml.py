import time
import os
import json
import numpy as np
from sklearn.ensemble import RandomForestClassifier

class MLModule:
    def __init__(self, cfg: dict, reporter, state_file="ml_data.json", min_examples=5):
        self.cfg = cfg
        self.reporter = reporter
        self.model = RandomForestClassifier(n_estimators=100, random_state=42)
        self.trained = False
        self.last_train = 0
        self.state_file = state_file
        self.min_examples = min_examples  # الحد الأدنى لتشغيل التدريب

        # load past data if exists
        self.data = {"features": [], "labels": []}
        self._load_data()

    def _load_data(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.reporter.log("[ML] Failed to load training data")

    def _save_data(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except Exception as e:
            self.reporter.log(f"[ML] Failed to save data: {e}")

    def add_training_example(self, features, label):
        """أضف عينة تدريب جديدة"""
        self.data["features"].append(features)
        self.data["labels"].append(label)
        self._save_data()
        self.reporter.log(f"[ML] Added training example. Total={len(self.data['labels'])}")

    def prepare_training_data(self):
        X = np.array(self.data["features"])
        y = np.array(self.data["labels"])
        if len(y) == 0:
            return None, None
        return X, y

    def train_if_needed(self):
        now = time.time()
        period = self.cfg.get("ml", {}).get("retrain_every_hours", 24)
        if self.trained and now - self.last_train < period * 3600:
            return

        X, y = self.prepare_training_data()
        if X is None or y is None or len(y) < self.min_examples:
            self.reporter.log(f"[ML] Not enough data to train. Current={len(y) if y is not None else 0}")
            return

        self.model.fit(X, y)
        self.trained = True
        self.last_train = now
        self.reporter.log(f"[ML] Model trained successfully with {len(y)} examples")

    def predict(self, features):
        if not self.trained:
            return 0
        try:
            return self.model.predict([features])[0]
        except Exception as e:
            self.reporter.log(f"[ML] Prediction error: {e}")
            return 0
