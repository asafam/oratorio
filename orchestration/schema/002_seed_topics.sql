-- Seed topics for the 5 orchestration roles (evaluator, monitor, dataset,
-- experiment-manager, overseer). Extend this list as new roles/workflows
-- need dedicated channels -- keep topic names short, lowercase, hyphenated.

INSERT INTO board.topic (topic, description) VALUES
    ('eval-requests',    'Requests to run an evaluation'),
    ('eval-results',     'Evaluation run progress and final results'),
    ('dataset-updates',  'Dataset generation/curation changes and requests'),
    ('monitor-alerts',   'Anomalies/health issues flagged by or to the monitor role'),
    ('overseer-alerts',  'Items other agents proactively flag to the overseer'),
    ('general',          'Catch-all topic every role subscribes to by default')
ON CONFLICT (topic) DO NOTHING;
