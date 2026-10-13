"""Export utilities for product history and statistics."""
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


class DataExporter:
    """Export product data to various formats."""
    
    def __init__(self, export_dir: str = "exports"):
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(exist_ok=True)
    
    def export_to_csv(self, products: List, filename: Optional[str] = None) -> str:
        """
        Export products to CSV file.
        
        Args:
            products: List of Product objects
            filename: Output filename (auto-generated if None)
        
        Returns:
            Path to exported file
        """
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"festool_products_{timestamp}.csv"
        
        filepath = self.export_dir / filename
        
        try:
            with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = [
                    'item_number', 'name', 'sale_price', 'regular_price',
                    'collection', 'posted_at', 'replaced_at', 'minutes_live',
                    'description'
                ]
                
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                
                for product in products:
                    writer.writerow({
                        'item_number': product.item_number,
                        'name': product.name,
                        'sale_price': product.sale_price,
                        'regular_price': product.regular_price or '',
                        'collection': product.collection_handle or 'rotation',
                        'posted_at': product.posted_at.isoformat() if product.posted_at else '',
                        'replaced_at': product.replaced_at.isoformat() if product.replaced_at else '',
                        'minutes_live': ('' if product.minutes_live is None
                                         else product.minutes_live),
                        'description': (product.description or '')[:500]  # Truncate long descriptions
                    })
            
            logger.info(f"Exported {len(products)} products to {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Error exporting to CSV: {e}")
            raise
    
    def export_to_json(self, products: List, filename: Optional[str] = None) -> str:
        """
        Export products to JSON file.
        
        Args:
            products: List of Product objects
            filename: Output filename (auto-generated if None)
        
        Returns:
            Path to exported file
        """
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"festool_products_{timestamp}.json"
        
        filepath = self.export_dir / filename
        
        try:
            data = []
            for product in products:
                data.append({
                    'item_number': product.item_number,
                    'name': product.name,
                    'sale_price': product.sale_price,
                    'regular_price': product.regular_price,
                    'collection': product.collection_handle or 'rotation',
                    'posted_at': product.posted_at.isoformat() if product.posted_at else None,
                    'replaced_at': product.replaced_at.isoformat() if product.replaced_at else None,
                    'minutes_live': product.minutes_live,
                    'description': product.description,
                    'image_url': product.image_url
                })
            
            with open(filepath, 'w', encoding='utf-8') as jsonfile:
                json.dump(data, jsonfile, indent=2, ensure_ascii=False)
            
            logger.info(f"Exported {len(products)} products to {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Error exporting to JSON: {e}")
            raise
    
    def export_to_excel(self, products: List, filename: Optional[str] = None) -> str:
        """
        Export products to Excel file.
        
        Args:
            products: List of Product objects
            filename: Output filename (auto-generated if None)
        
        Returns:
            Path to exported file
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("pandas and openpyxl required for Excel export. "
                            "Install with: pip install pandas openpyxl")
        
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"festool_products_{timestamp}.xlsx"
        
        filepath = self.export_dir / filename
        
        try:
            data = []
            for product in products:
                data.append({
                    'Item Number': product.item_number,
                    'Name': product.name,
                    'Sale Price': product.sale_price,
                    'Regular Price': product.regular_price or '',
                    'Collection': product.collection_handle or 'rotation',
                    'Posted At': product.posted_at,
                    'Replaced At': product.replaced_at,
                    'Minutes Live': ('' if product.minutes_live is None
                                     else product.minutes_live),
                    'Description': product.description or ''
                })
            
            df = pd.DataFrame(data)
            
            with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Products', index=False)
                
                # Auto-adjust column widths
                worksheet = writer.sheets['Products']
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = column[0].column_letter
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except:
                            pass
                    adjusted_width = min(max_length + 2, 50)
                    worksheet.column_dimensions[column_letter].width = adjusted_width
            
            logger.info(f"Exported {len(products)} products to {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Error exporting to Excel: {e}")
            raise
    
    def export_statistics(self, stats: dict, filename: Optional[str] = None) -> str:
        """
        Export statistics to JSON file.
        
        Args:
            stats: Statistics dictionary
            filename: Output filename (auto-generated if None)
        
        Returns:
            Path to exported file
        """
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"festool_stats_{timestamp}.json"
        
        filepath = self.export_dir / filename
        
        try:
            with open(filepath, 'w', encoding='utf-8') as jsonfile:
                json.dump(stats, jsonfile, indent=2)
            
            logger.info(f"Exported statistics to {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Error exporting statistics: {e}")
            raise


class ReportGenerator:
    """Generate formatted reports from product data."""
    
    def __init__(self, export_dir: str = "exports"):
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(exist_ok=True)
    
    def generate_html_report(self, products: List, stats: dict, 
                            filename: Optional[str] = None) -> str:
        """
        Generate an HTML report with products and statistics.
        
        Args:
            products: List of Product objects
            stats: Statistics dictionary
            filename: Output filename (auto-generated if None)
        
        Returns:
            Path to exported file
        """
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"festool_report_{timestamp}.html"
        
        filepath = self.export_dir / filename
        
        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Festool Recon Monitor Report</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            background-color: #009a44;
            color: white;
            padding: 20px;
            border-radius: 5px;
            margin-bottom: 20px;
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: white;
            padding: 20px;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .stat-value {{
            font-size: 2em;
            font-weight: bold;
            color: #009a44;
        }}
        .stat-label {{
            color: #666;
            margin-top: 5px;
        }}
        table {{
            width: 100%;
            background: white;
            border-collapse: collapse;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        th {{
            background-color: #009a44;
            color: white;
            padding: 12px;
            text-align: left;
        }}
        td {{
            padding: 10px;
            border-bottom: 1px solid #ddd;
        }}
        tr:hover {{
            background-color: #f9f9f9;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            color: #666;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Festool Recon Monitor Report</h1>
        <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>
    
    <div class="stats">
        <div class="stat-card">
            <div class="stat-value">{stats.get('total_products', 0)}</div>
            <div class="stat-label">Total Products</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats.get('average_minutes_live', 0):.0f}</div>
            <div class="stat-label">Avg Minutes Live</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{stats.get('total_notifications', 0)}</div>
            <div class="stat-label">Notifications Sent</div>
        </div>
    </div>
    
    <h2>Product History</h2>
    <table>
        <thead>
            <tr>
                <th>Item #</th>
                <th>Name</th>
                <th>Price</th>
                <th>Posted</th>
                <th>Live Time</th>
            </tr>
        </thead>
        <tbody>
"""
        
        for product in products:
            posted = product.posted_at.strftime('%Y-%m-%d %H:%M') if product.posted_at else 'N/A'
            # Whether a listing is still up is replaced_at's job, not a
            # duration's: an item that sold out in under a minute has
            # minutes_live == 0, and reading that as falsy reported a finished
            # listing as "Current".
            if product.replaced_at is None:
                minutes = "Current"
            elif product.minutes_live is None:
                minutes = "Unknown"
            else:
                minutes = f"{product.minutes_live} min"
            
            html_content += f"""
            <tr>
                <td>{product.item_number}</td>
                <td>{product.name}</td>
                <td>${product.sale_price:.2f}</td>
                <td>{posted}</td>
                <td>{minutes}</td>
            </tr>
"""
        
        html_content += """
        </tbody>
    </table>
    
    <div class="footer">
        <p>Festool Recon Monitor - Product Tracking and Notifications</p>
    </div>
</body>
</html>
"""
        
        try:
            with open(filepath, 'w', encoding='utf-8') as htmlfile:
                htmlfile.write(html_content)
            
            logger.info(f"Generated HTML report: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"Error generating HTML report: {e}")
            raise