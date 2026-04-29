CREATE TABLE prompts (
  id SERIAL PRIMARY KEY,
  total_score INTEGER NOT NULL CHECK (total_score >= 0)
);
