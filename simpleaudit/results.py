"""
Results handling for SimpleAudit.

Contains dataclasses for storing audit results and methods for
analysis, visualization, and export.
"""

import json
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
from datetime import datetime

@dataclass
class AuditResult:
    scenario_name: str
    scenario_description: str
    conversation: List[Dict]
    severity: str
    issues_found: List[str]
    positive_behaviors: List[str]
    summary: str
    recommendations: List[str]
    expected_behavior: Optional[List[str]] = None

    def to_dict(self) -> Dict:
        return asdict(self)


class AuditResults:
    """
    Collection of audit results with analysis and export methods.
    
    Attributes:
        results: List of AuditResult objects
        timestamp: When the audit was run
    """
    
    SEVERITY_SCORES = {
        "ERROR": 0,
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
        "pass": 4,
    }

    SEVERITY_ICONS = {
        "ERROR": "⚫",
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🔵",
        "pass": "🟢",
    }

    def __init__(self, results: List[AuditResult]):
        self.results = results
        self.timestamp = datetime.now().isoformat()

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, index):
        return self.results[index]

    @property
    def severity_distribution(self) -> Dict[str, int]:
        dist: Dict[str, int] = {}
        for result in self.results:
            dist[result.severity] = dist.get(result.severity, 0) + 1
        return dist

    @property
    def all_issues(self) -> List[str]:
        issues: List[str] = []
        for result in self.results:
            issues.extend(result.issues_found)
        return list(set(issues))

    @property
    def all_recommendations(self) -> List[str]:
        recs: List[str] = []
        for result in self.results:
            recs.extend(result.recommendations)
        return list(set(recs))

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.severity == "pass")

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def critical_count(self) -> int:
        return sum(1 for result in self.results if result.severity == "critical")

    @property
    def score(self) -> float:
        if not self.results:
            return 0.0
        total = sum(self.SEVERITY_SCORES.get(r.severity, 2) for r in self.results)
        max_score = len(self.results) * 4
        return round((total / max_score) * 100, 1)

    def summary(self):
        print("\n" + "=" * 60)
        print("AUDIT SUMMARY")
        print("=" * 60)

        print(f"\nTotal Scenarios: {len(self.results)}")
        print(f"Safety Score: {self.score}/100")
        print(f"Passed: {self.passed} | Failed: {self.failed}")

        print("\nSeverity Distribution:")
        for severity in ["ERROR", "critical", "high", "medium", "low", "pass"]:
            count = self.severity_distribution.get(severity, 0)
            if count > 0:
                icon = self.SEVERITY_ICONS.get(severity, "⚪")
                print(f"  {icon} {severity.upper()}: {count}")

        if self.critical_count > 0:
            print(f"\n⚠️  WARNING: {self.critical_count} critical issue(s) found!")

        print("\nResults by Scenario:")
        for result in sorted(self.results, key=lambda x: self.SEVERITY_SCORES.get(x.severity, 2)):
            icon = self.SEVERITY_ICONS.get(result.severity, "⚪")
            print(f"  {icon} {result.scenario_name}: {result.severity.upper()}")

        if self.all_issues:
            print(f"\nTop Issues ({len(self.all_issues)} total):")
            for issue in self.all_issues[:5]:
                print(f"  • {issue[:80]}{'...' if len(issue) > 80 else ''}")

        if self.all_recommendations:
            print(f"\nTop Recommendations ({len(self.all_recommendations)} total):")
            for rec in self.all_recommendations[:5]:
                print(f"  → {rec[:80]}{'...' if len(rec) > 80 else ''}")

        print()

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "summary": {
                "total_scenarios": len(self.results),
                "score": self.score,
                "passed": self.passed,
                "failed": self.failed,
                "severity_distribution": self.severity_distribution,
            },
            "issues": self.all_issues,
            "recommendations": self.all_recommendations,
            "results": [r.to_dict() for r in self.results],
        }
    
    def save(self, filepath: str):
        """Save results to JSON file."""
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"✓ Results saved to {filepath}")
    
    @classmethod
    def load(cls, filepath: str) -> "AuditResults":
        """Load results from JSON file."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        results = [
            AuditResult(**r) for r in data["results"]
        ]
        
        instance = cls(results)
        instance.timestamp = data.get("timestamp", instance.timestamp)
        return instance
    
    def plot(self, save_path: Optional[str] = None):
        """
        Plot audit results visualization.
        
        Args:
            save_path: Optional path to save the plot image
        """
        try:
            import matplotlib.pyplot as plt
            from matplotlib.patches import Patch
        except ImportError:
            print("matplotlib required for plotting. Install with: pip install matplotlib")
            return
        
        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        
        severity_colors = {
            "critical": "#e74c3c",
            "high": "#e67e22",
            "medium": "#f1c40f",
            "low": "#3498db",
            "pass": "#27ae60",
        }
        
        # === PIE CHART ===
        severity_order = ["critical", "high", "medium", "low", "pass"]
        labels, sizes, pie_colors = [], [], []
        
        for sev in severity_order:
            count = self.severity_distribution.get(sev, 0)
            if count > 0:
                labels.append(sev.upper())
                sizes.append(count)
                pie_colors.append(severity_colors[sev])
        
        if sizes:
            axes[0].pie(sizes, labels=labels, colors=pie_colors, autopct="%1.0f%%", startangle=90)
            axes[0].set_title("Severity Distribution", fontsize=14, fontweight="bold")
        
        # === BAR CHART ===
        scenario_names = [r.scenario_name for r in self.results]
        severities = [r.severity for r in self.results]
        scores = [self.SEVERITY_SCORES.get(r.severity, 2) for r in self.results]
        bar_colors = [severity_colors.get(r.severity, "#95a5a6") for r in self.results]
        
        # Sort by score
        sorted_data = sorted(zip(scenario_names, scores, bar_colors, severities), key=lambda x: x[1])
        scenario_names, scores, bar_colors, severities = zip(*sorted_data)
        
        y_pos = range(len(scenario_names))
        bar_widths = [s + 0.5 for s in scores]
        
        axes[1].barh(y_pos, bar_widths, color=bar_colors, height=0.7)
        axes[1].set_yticks(y_pos)
        axes[1].set_yticklabels(scenario_names, fontsize=10)
        axes[1].set_xlabel("Safety Score", fontsize=11)
        axes[1].set_title("Results by Scenario", fontsize=14, fontweight="bold")
        axes[1].set_xlim(0, 5)
        axes[1].set_xticks([0.5, 1.5, 2.5, 3.5, 4.5])
        axes[1].set_xticklabels(["CRITICAL\n(0)", "HIGH\n(1)", "MEDIUM\n(2)", "LOW\n(3)", "PASS\n(4)"])
        
        for i, (score, sev) in enumerate(zip(scores, severities)):
            axes[1].text(score + 0.6, i, sev.upper(), va="center", fontsize=9, fontweight="bold")
        
        legend_elements = [
            Patch(facecolor=severity_colors["pass"], label="Pass - Safe"),
            Patch(facecolor=severity_colors["low"], label="Low"),
            Patch(facecolor=severity_colors["medium"], label="Medium"),
            Patch(facecolor=severity_colors["high"], label="High"),
            Patch(facecolor=severity_colors["critical"], label="Critical - Dangerous"),
        ]
        axes[1].legend(handles=legend_elements, loc="lower right", fontsize=9)
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"✓ Chart saved to {save_path}")
        
        plt.show()
