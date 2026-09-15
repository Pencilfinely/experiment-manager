"""Small CPU example with an ordinary CLI; no Experiment Manager dependency.

Fits y = weight*x + bias to a CSV so the complete integration example can run
with standard Python. Replace this file with your real algorithm's CLI.
"""
import argparse
import csv
import json
from pathlib import Path
import random


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--lr', type=float, default=0.05)
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with (Path(args.data_dir) / 'train.csv').open(newline='') as stream:
        points = [(float(row['x']), float(row['y'])) for row in csv.DictReader(stream)]
    if not points or args.epochs < 1 or args.lr <= 0:
        raise ValueError('Expected nonempty data, positive epochs and learning rate')
    rng = random.Random(args.seed)
    weight, bias = rng.random(), 0.0
    for epoch in range(1, args.epochs + 1):
        dw = sum(2 * (weight*x + bias-y)*x for x, y in points) / len(points)
        db = sum(2 * (weight*x + bias-y) for x, y in points) / len(points)
        weight -= args.lr * dw
        bias -= args.lr * db
        loss = sum((weight*x + bias-y)**2 for x, y in points) / len(points)
        print(json.dumps({'epoch': epoch, 'loss': loss}), flush=True)
    (output / 'model.json').write_text(json.dumps({'weight': weight, 'bias': bias}))
    (output / 'summary.json').write_text(json.dumps({'loss': loss, 'epochs': args.epochs}))


if __name__ == '__main__':
    main()
