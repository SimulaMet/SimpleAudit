#!/usr/bin/env python3
"""
Example: Local Audit with Ollama

This example shows how to run a complete AI safety audit using only local
models via Ollama - no API keys or cloud services required!

Prerequisites:
1. Install Ollama: https://ollama.ai
2. Start Ollama: ollama serve
3. Pull a model: ollama pull llama3.2

Usage:
    python local_ollama_audit.py
"""

from simpleaudit import ModelAuditor, list_scenario_packs


def main():
    print("🦙 Local AI Safety Audit with Ollama")
    print("=" * 50)
    
    # Show available scenario packs
    print("\nAvailable scenario packs:")
    for pack, count in list_scenario_packs().items():
        print(f"  - {pack}: {count} scenarios")
    
    # Create auditor using Ollama for BOTH target and judge
    # This means everything runs locally - completely free!
    print("\n📋 Setting up local auditor...")
    
    # Configure your models here:
    TARGET_MODEL = "gemma3"      # Model to test (the one being audited)
    JUDGE_MODEL = "gemma3"       # Model to judge responses (can be same or different)
    
    # Other popular options if you have them:
    # TARGET_MODEL = "llama3.2"
    # TARGET_MODEL = "llama3:8b"
    # TARGET_MODEL = "deepseek-r1:8b"
    
    print(f"   Target model: {TARGET_MODEL} (via Ollama)")
    print(f"   Judge model: {JUDGE_MODEL} (via Ollama)")
    
    auditor = ModelAuditor(
        # Target model to test
        provider="ollama",
        model=TARGET_MODEL,
        system_prompt="You are a helpful AI assistant. Be concise and accurate.",
        
        # Judge model (same as target for fully local audit)
        # You could use a different/larger model for judging if available
        judge_provider="ollama",
        judge_model=JUDGE_MODEL,
        
        # Settings
        max_turns=3,  # Fewer turns for faster local testing
        verbose=True,
    )
    
    # Run a subset of safety scenarios
    print("\n🔍 Running safety audit (3 scenarios for demo)...")
    print("   This may take a few minutes with local models.\n")
    
    # Run just the first 3 safety scenarios for a quick demo
    from simpleaudit import get_scenarios
    scenarios = get_scenarios("safety")[:3]
    
    results = auditor.run(scenarios, max_turns=3)
    
    # Display results
    print("\n" + "=" * 50)
    print("📊 AUDIT RESULTS")
    print("=" * 50)
    results.summary()
    
    # Save results
    results.save("local_audit_results.json")
    print("\n✅ Results saved to: local_audit_results.json")
    
    # Optional: Try to plot if matplotlib is available
    try:
        results.plot(save_path="local_audit_chart.png")
        print("📈 Chart saved to: local_audit_chart.png")
    except ImportError:
        print("💡 Tip: Install matplotlib for charts: pip install simpleaudit[plot]")


if __name__ == "__main__":
    main()
