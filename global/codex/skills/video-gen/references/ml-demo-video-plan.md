# ML Demo Video Plan

Use this plan for an ML presentation video, especially when medical images and model explainability are involved.

## Evidence vs Generated Assets

Real evidence:
- screen recording of the tool,
- folder walkthrough,
- dataset split counts,
- training config and machine specs,
- validation images,
- model predictions,
- Grad-CAM or other explainability overlays computed from the actual model,
- metrics and confusion matrix from evaluation scripts.

Generated assets:
- intro/outro background,
- neutral transitions,
- abstract medical-imaging b-roll,
- animated title cards,
- non-factual visual polish around real screenshots.

Never generate fake dataset folders, fake metrics, fake heatmaps, or fake model outputs for a technical presentation.

## Suggested Storyboard

1. Title and goal: what the PoC detects/classifies, dataset size, model family.
2. Training setup: transfer-learning backbone, input size, epochs, optimizer, GPU/CPU/RAM.
3. Dataset walkthrough: show real folder tree and counts.
4. Split: train/val/test counts and class balance.
5. Validation demo: choose representative validation images, show true label, prediction, confidence.
6. Explainability: original ultrasound, heatmap, overlay, short caveat that Grad-CAM is interpretability support, not clinical proof.
7. Summary: accuracy/F1/AUC/confusion matrix plus limitations and next work.

## Recommended Generated Clips

- 3-4 s clean opener with abstract ultrasound texture and soft grid, no text.
- 2-3 s transition from folder tree to split chart, no fake filenames.
- 3-4 s transition from validation samples to heatmap overlay, using a real first frame if possible.
- 3-4 s closing background for summary metrics.

## Prompt Rules

- Avoid generating readable metrics or UI text inside video. Add text in PowerPoint/video editor.
- For generated medical visuals, use abstract or clearly synthetic framing unless based on a real first frame.
- For first-frame animations, require: preserve exact content, no new labels, no altered anatomy, no fake annotation.
- Keep clips short; make the editing timeline carry the story.
