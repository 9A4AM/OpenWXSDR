"""
Utility script to test OpenWXSDR components independently
"""

import sys
import os
import yaml
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


def load_config():
    """Load configuration"""
    with open('config.yaml', 'r') as f:
        return yaml.safe_load(f)


def test_rtlsdr():
    """Test RTL-SDR connection and spectrum analyzer"""
    print("\n=== Testing RTL-SDR ===\n")
    
    from src.sdr.rtlsdr_analyzer import SpectrumAnalyzer
    
    config = load_config()
    analyzer = SpectrumAnalyzer(config)
    
    if not analyzer.initialize():
        print("✗ Failed to initialize RTL-SDR")
        return False
    
    print("✓ RTL-SDR initialized successfully")
    
    # Capture spectrum
    print("Capturing spectrum...")
    try:
        freqs, power = analyzer.capture_spectrum()
        print(f"✓ Captured {len(freqs)} frequency bins")
        print(f"  Frequency range: {freqs[0]/1e6:.2f} - {freqs[-1]/1e6:.2f} MHz")
        print(f"  Power range: {power.min():.1f} - {power.max():.1f} dB")
        
        # Detect signals
        signals = analyzer.detect_signals(freqs, power)
        print(f"✓ Detected {len(signals)} potential signals")
        
        for sig in signals[:5]:  # Show first 5
            print(f"  - {sig.frequency/1e6:.4f} MHz, SNR: {sig.strength:.1f} dB, BW: {sig.bandwidth/1e3:.1f} kHz")
        
    except Exception as e:
        print(f"✗ Error during spectrum capture: {e}")
        return False
    finally:
        analyzer.close()
    
    return True


def test_decoders():
    """Test decoder binaries"""
    print("\n=== Testing Decoders ===\n")
    
    config = load_config()
    decoders_path = config['decoders']['rs1729_path']
    
    from src.decoders.rs1729_decoder import DECODER_BINARIES
    
    found = 0
    missing = []
    
    for sonde_type, binary in DECODER_BINARIES.items():
        binary_path = os.path.join(decoders_path, binary)
        
        if os.path.exists(binary_path):
            print(f"✓ {sonde_type}: {binary_path}")
            found += 1
        else:
            print(f"✗ {sonde_type}: NOT FOUND at {binary_path}")
            missing.append(sonde_type)
    
    print(f"\nFound {found}/{len(DECODER_BINARIES)} decoders")
    
    if missing:
        print(f"Missing: {', '.join(missing)}")
        print("\nRun install.sh to build decoders")
        return False
    
    return True


def test_webui():
    """Test web UI"""
    print("\n=== Testing Web UI ===\n")
    
    from src.webui.web_server import WebUI
    
    config = load_config()
    webui = WebUI(config)
    
    print(f"✓ Web UI initialized")
    print(f"  Host: {webui.host}")
    print(f"  Port: {webui.port}")
    print(f"  Enabled: {webui.enabled}")
    
    return True


def test_udp_output():
    """Test UDP output"""
    print("\n=== Testing UDP Output ===\n")
    
    from src.output.udp_output import UDPOutput
    from src.decoders.models import SondeTelemetry, SondePosition
    from datetime import datetime
    
    config = load_config()
    output = UDPOutput(config)
    
    print(f"✓ UDP output initialized")
    print(f"  Host: {output.host}")
    print(f"  Port: {output.port}")
    print(f"  Enabled: {output.enabled}")
    
    # Create test telemetry
    telemetry = SondeTelemetry(
        sonde_type='RS41',
        serial='T1234567',
        frame_number=100,
        frequency=402.7e6,
        snr=25.5
    )
    
    telemetry.position = SondePosition(
        latitude=51.5074,
        longitude=-0.1278,
        altitude=15420.5,
        datetime=datetime.utcnow()
    )
    
    # Test payload generation
    payload = output._build_openwx_payload(telemetry)
    print(f"✓ Generated OpenWX payload:")
    
    import json
    print(json.dumps(payload, indent=2))
    
    output.close()
    return True


def main():
    """Run all tests"""
    print("=" * 60)
    print("  OpenWXSDR Component Tests")
    print("=" * 60)
    
    tests = [
        ("Decoders", test_decoders),
        ("RTL-SDR", test_rtlsdr),
        ("Web UI", test_webui),
        ("UDP Output", test_udp_output),
    ]
    
    results = {}
    
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"\n✗ {name} test failed with exception: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False
    
    # Summary
    print("\n" + "=" * 60)
    print("  Test Summary")
    print("=" * 60)
    
    for name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")
    
    print()
    
    all_passed = all(results.values())
    if all_passed:
        print("All tests passed! ✓")
        return 0
    else:
        print("Some tests failed! ✗")
        return 1


if __name__ == '__main__':
    sys.exit(main())
