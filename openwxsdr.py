#!/usr/bin/env python3
"""
OpenWXSDR - Main Entry Point
Streamlined Radiosonde Decoder Framework
"""

import sys
import os
import logging
import yaml
import argparse
from pathlib import Path

# Add src directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from src.openwxsdr_app import OpenWXSDR


def setup_logging(config: dict):
    """Setup logging configuration"""
    log_config = config.get('logging', {})
    log_level = getattr(logging, log_config.get('level', 'INFO'))
    log_file = log_config.get('file', 'logs/openwxsdr.log')
    
    # Create logs directory
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    
    # Configure logging
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )


def load_config(config_path: str = 'config.yaml') -> dict:
    """Load configuration from YAML file"""
    try:
        # Get absolute path to config file
        config_path = os.path.abspath(config_path)
        config_dir = os.path.dirname(config_path)
        
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        # Resolve relative paths in config to absolute paths
        # This is critical for systemd services where working directory may differ
        if 'decoders' in config and 'rs1729_path' in config['decoders']:
            rs1729_path = config['decoders']['rs1729_path']
            if not os.path.isabs(rs1729_path):
                # Resolve relative to config directory
                config['decoders']['rs1729_path'] = os.path.abspath(
                    os.path.join(config_dir, rs1729_path)
                )
        
        if 'logging' in config and 'file' in config['logging']:
            log_file = config['logging']['file']
            if not os.path.isabs(log_file):
                config['logging']['file'] = os.path.abspath(
                    os.path.join(config_dir, log_file)
                )
        
        if 'output' in config and 'log' in config['output']:
            if 'path' in config['output']['log']:
                log_path = config['output']['log']['path']
                if not os.path.isabs(log_path):
                    config['output']['log']['path'] = os.path.abspath(
                        os.path.join(config_dir, log_path)
                    )
        
        return config
    except FileNotFoundError:
        print(f"Error: Configuration file '{config_path}' not found!")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing configuration file: {e}")
        sys.exit(1)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='OpenWXSDR - Streamlined Radiosonde Decoder Framework',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python openwxsdr.py                   # Run with default config.yaml
  python openwxsdr.py -c custom.yaml    # Run with custom config
  python openwxsdr.py --test-sdr        # Test SDR connection
        """
    )
    
    parser.add_argument(
        '-c', '--config',
        default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )
    
    parser.add_argument(
        '--test-sdr',
        action='store_true',
        help='Test SDR connection and exit'
    )
    
    parser.add_argument(
        '--version',
        action='version',
        version='OpenWXSDR v1.0.45'
    )
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_config(args.config)
    
    # Setup logging
    setup_logging(config)
    logger = logging.getLogger('main')
    
    # Print banner
    print("=" * 60)
    print("  OpenWXSDR - Streamlined Radiosonde Decoder Framework")
    print("  Version 1.0.45")
    print("=" * 60)
    print()
    
    # Test SDR mode
    if args.test_sdr:
        logger.info("Testing SDR connection...")
        from src.sdr.rtlsdr_analyzer import SpectrumAnalyzer
        
        analyzer = SpectrumAnalyzer(config)
        if analyzer.initialize():
            logger.info("✓ SDR connection successful!")
            analyzer.close()
            sys.exit(0)
        else:
            logger.error("✗ SDR connection failed!")
            sys.exit(1)
    
    # Create and run application
    try:
        app = OpenWXSDR(config)
        
        if not app.initialize():
            logger.error("Failed to initialize application")
            sys.exit(1)
        
        # Start application
        app.start()
        
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
