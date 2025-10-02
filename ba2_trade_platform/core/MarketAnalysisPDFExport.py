"""
MarketAnalysisPDFExport - PDF Export functionality for Market Analysis

This module provides functionality to export market analysis data to PDF format,
including general analysis information, order recommendations, and expert-rendered content.
"""

from typing import Optional, Dict, Any, List, Tuple
import io
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.colors import HexColor, black, white, grey
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    from reportlab.platypus.flowables import HRFlowable
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

from sqlmodel import select
from ..core.db import get_instance, get_db
from ..core.models import MarketAnalysis, ExpertInstance, AnalysisOutput, Instrument, TradingOrder, ExpertRecommendation
from ..core.types import MarketAnalysisStatus, OrderStatus
from ..logger import logger


class MarketAnalysisPDFExport:
    """
    Handles PDF export of market analysis data including recommendations and expert content.
    """
    
    def __init__(self):
        """Initialize the PDF export handler."""
        if not REPORTLAB_AVAILABLE:
            raise ImportError("reportlab is required for PDF export. Install with: pip install reportlab")
        
        # Set up styles
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
    
    def _setup_custom_styles(self):
        """Set up custom paragraph styles for the PDF."""
        # Title style
        self.styles.add(ParagraphStyle(
            name='CustomTitle',
            parent=self.styles['Title'],
            fontSize=24,
            spaceAfter=20,
            textColor=HexColor('#2E3440'),
            alignment=TA_CENTER
        ))
        
        # Subtitle style
        self.styles.add(ParagraphStyle(
            name='CustomSubtitle',
            parent=self.styles['Heading2'],
            fontSize=16,
            spaceAfter=12,
            textColor=HexColor('#5E81AC'),
            leftIndent=0
        ))
        
        # Section heading style
        self.styles.add(ParagraphStyle(
            name='SectionHeading',
            parent=self.styles['Heading3'],
            fontSize=14,
            spaceAfter=8,
            spaceBefore=16,
            textColor=HexColor('#434C5E'),
            leftIndent=0
        ))
        
        # Info text style
        self.styles.add(ParagraphStyle(
            name='InfoText',
            parent=self.styles['Normal'],
            fontSize=10,
            textColor=HexColor('#4C566A'),
            spaceAfter=6
        ))
        
        # Code/monospace style
        self.styles.add(ParagraphStyle(
            name='CodeText',
            parent=self.styles['Normal'],
            fontSize=9,
            fontName='Courier',
            textColor=HexColor('#2E3440'),
            leftIndent=20,
            rightIndent=20,
            spaceAfter=6,
            spaceBefore=6
        ))
    
    def export_analysis_to_pdf(self, analysis_id: int, output_path: Optional[str] = None) -> str:
        """
        Export a market analysis to PDF format.
        
        Args:
            analysis_id: The ID of the MarketAnalysis to export
            output_path: Optional path for the output file. If None, a default path will be generated.
            
        Returns:
            The path to the generated PDF file
            
        Raises:
            ValueError: If the analysis is not found
            Exception: If PDF generation fails
        """
        try:
            # Load the market analysis
            market_analysis = get_instance(MarketAnalysis, analysis_id)
            if not market_analysis:
                raise ValueError(f"Market Analysis {analysis_id} not found")
            
            # Generate output path if not provided
            if output_path is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"market_analysis_{market_analysis.symbol}_{analysis_id}_{timestamp}.pdf"
                output_path = str(Path.home() / "Documents" / "ba2_trade_platform" / "exports" / filename)
            
            # Ensure output directory exists
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            
            # Create PDF document
            doc = SimpleDocTemplate(output_path, pagesize=A4)
            story = []
            
            # Build PDF content
            self._add_header(story, market_analysis)
            self._add_analysis_overview(story, market_analysis)
            self._add_recommendations_section(story, market_analysis)
            self._add_expert_content_section(story, market_analysis)
            self._add_footer(story, market_analysis)
            
            # Build the PDF
            doc.build(story)
            
            logger.info(f"Successfully exported market analysis {analysis_id} to PDF: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"Error exporting market analysis {analysis_id} to PDF: {e}", exc_info=True)
            raise
    
    def _add_header(self, story: List, market_analysis: MarketAnalysis):
        """Add the PDF header with title and basic info."""
        try:
            # Get instrument details
            instrument = self._get_instrument_details(market_analysis.symbol)
            
            # Main title
            title_text = f"Market Analysis Report - {market_analysis.symbol}"
            if instrument and instrument.company_name:
                title_text += f" ({instrument.company_name})"
            
            story.append(Paragraph(title_text, self.styles['CustomTitle']))
            story.append(Spacer(1, 20))
            
            # Analysis info table
            analysis_info = [
                ['Analysis ID:', str(market_analysis.id)],
                ['Symbol:', market_analysis.symbol],
                ['Status:', market_analysis.status.value if market_analysis.status else 'Unknown'],
                ['Created:', self._format_datetime(market_analysis.created_at)],
                ['Use Case:', market_analysis.subtype.value if market_analysis.subtype else 'Unknown']
            ]
            
            # Add instrument info if available
            if instrument:
                if instrument.company_name:
                    analysis_info.append(['Company:', instrument.company_name])
                if instrument.categories:
                    analysis_info.append(['Sector:', ', '.join(instrument.categories)])
                if instrument.labels:
                    analysis_info.append(['Labels:', ', '.join(instrument.labels)])
            
            info_table = Table(analysis_info, colWidths=[1.5*inch, 4*inch])
            info_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
            ]))
            
            story.append(info_table)
            story.append(Spacer(1, 20))
            story.append(HRFlowable(width="100%", thickness=1, lineCap='round', color=HexColor('#E5E9F0')))
            story.append(Spacer(1, 20))
            
        except Exception as e:
            logger.error(f"Error adding PDF header: {e}", exc_info=True)
            story.append(Paragraph("Error generating header", self.styles['Normal']))
    
    def _add_analysis_overview(self, story: List, market_analysis: MarketAnalysis):
        """Add analysis overview section."""
        try:
            story.append(Paragraph("Analysis Overview", self.styles['CustomSubtitle']))
            
            # Expert instance info
            expert_instance = get_instance(ExpertInstance, market_analysis.expert_instance_id)
            if expert_instance:
                expert_info = [
                    ['Expert:', expert_instance.expert],
                    ['Expert ID:', str(expert_instance.id)],
                    ['Enabled:', 'Yes' if expert_instance.enabled else 'No'],
                    ['Virtual Equity:', f"{expert_instance.virtual_equity_pct}%"],
                ]
                
                if expert_instance.user_description:
                    expert_info.append(['Description:', expert_instance.user_description])
                
                expert_table = Table(expert_info, colWidths=[1.5*inch, 4*inch])
                expert_table.setStyle(TableStyle([
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                    ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                ]))
                
                story.append(expert_table)
            
            # Analysis status details
            if market_analysis.status == MarketAnalysisStatus.FAILED:
                error_msg = self._extract_error_message(market_analysis.state)
                if error_msg:
                    story.append(Spacer(1, 12))
                    story.append(Paragraph("Error Details", self.styles['SectionHeading']))
                    story.append(Paragraph(error_msg, self.styles['CodeText']))
            
            story.append(Spacer(1, 20))
            
        except Exception as e:
            logger.error(f"Error adding analysis overview: {e}", exc_info=True)
            story.append(Paragraph("Error generating analysis overview", self.styles['Normal']))
    
    def _add_recommendations_section(self, story: List, market_analysis: MarketAnalysis):
        """Add recommendations and orders section."""
        try:
            story.append(Paragraph("Trading Recommendations & Orders", self.styles['CustomSubtitle']))
            
            # Get recommendations
            session = get_db()
            statement = select(ExpertRecommendation).where(
                ExpertRecommendation.market_analysis_id == market_analysis.id
            )
            recommendations = list(session.exec(statement).all())
            
            if not recommendations:
                story.append(Paragraph("No recommendations generated from this analysis.", self.styles['InfoText']))
                session.close()
                return
            
            # Recommendations summary
            rec_counts = {'BUY': 0, 'SELL': 0, 'HOLD': 0}
            for rec in recommendations:
                action = rec.recommended_action.value if hasattr(rec.recommended_action, 'value') else str(rec.recommended_action)
                rec_counts[action] = rec_counts.get(action, 0) + 1
            
            summary_text = f"Generated {len(recommendations)} recommendation(s): "
            summary_parts = []
            for action, count in rec_counts.items():
                if count > 0:
                    summary_parts.append(f"{count} {action}")
            summary_text += ", ".join(summary_parts)
            
            story.append(Paragraph(summary_text, self.styles['InfoText']))
            story.append(Spacer(1, 12))
            
            # Recommendations table
            if recommendations:
                rec_data = [['Symbol', 'Action', 'Confidence', 'Expected Profit', 'Risk Level', 'Time Horizon']]
                
                for rec in recommendations:
                    action = rec.recommended_action.value if hasattr(rec.recommended_action, 'value') else str(rec.recommended_action)
                    confidence = f"{rec.confidence:.1f}%" if rec.confidence is not None else 'N/A'
                    profit = f"{rec.expected_profit_percent:.2f}%" if rec.expected_profit_percent else 'N/A'
                    risk = rec.risk_level.value if hasattr(rec.risk_level, 'value') else str(rec.risk_level)
                    horizon = rec.time_horizon.value.replace('_', ' ').title() if hasattr(rec.time_horizon, 'value') else str(rec.time_horizon)
                    
                    rec_data.append([
                        rec.symbol,
                        action,
                        confidence,
                        profit,
                        risk,
                        horizon
                    ])
                
                rec_table = Table(rec_data, colWidths=[0.8*inch, 0.8*inch, 0.8*inch, 1*inch, 0.8*inch, 1*inch])
                rec_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), HexColor('#5E81AC')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 6),
                    ('GRID', (0, 0), (-1, -1), 1, black),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, HexColor('#F8F9FA')])
                ]))
                
                story.append(rec_table)
            
            # Get related orders
            recommendation_ids = [rec.id for rec in recommendations]
            orders_statement = select(TradingOrder).where(
                TradingOrder.expert_recommendation_id.in_(recommendation_ids)
            ).order_by(TradingOrder.created_at.desc())
            orders = list(session.exec(orders_statement).all())
            
            session.close()
            
            if orders:
                story.append(Spacer(1, 16))
                story.append(Paragraph("Created Orders", self.styles['SectionHeading']))
                
                order_data = [['Symbol', 'Side', 'Quantity', 'Type', 'Status', 'Limit Price']]
                
                for order in orders:
                    status = order.status.value if hasattr(order.status, 'value') else str(order.status)
                    quantity = f"{order.quantity:.2f}" if order.quantity else ''
                    limit_price = f"${order.limit_price:.2f}" if order.limit_price else ''
                    
                    order_data.append([
                        order.symbol,
                        order.side,
                        quantity,
                        order.order_type,
                        status,
                        limit_price
                    ])
                
                order_table = Table(order_data, colWidths=[0.8*inch, 0.8*inch, 0.8*inch, 0.8*inch, 0.8*inch, 1*inch])
                order_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), HexColor('#5E81AC')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, -1), 9),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 6),
                    ('GRID', (0, 0), (-1, -1), 1, black),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, HexColor('#F8F9FA')])
                ]))
                
                story.append(order_table)
            
            story.append(Spacer(1, 20))
            
        except Exception as e:
            logger.error(f"Error adding recommendations section: {e}", exc_info=True)
            story.append(Paragraph("Error generating recommendations section", self.styles['Normal']))
    
    def _add_expert_content_section(self, story: List, market_analysis: MarketAnalysis):
        """Add expert-specific content section."""
        try:
            story.append(Paragraph("Expert Analysis Content", self.styles['CustomSubtitle']))
            
            # Get analysis outputs
            session = get_db()
            statement = select(AnalysisOutput).where(
                AnalysisOutput.market_analysis_id == market_analysis.id
            ).order_by(AnalysisOutput.created_at)
            analysis_outputs = list(session.exec(statement).all())
            session.close()
            
            if not analysis_outputs:
                story.append(Paragraph("No detailed analysis outputs available.", self.styles['InfoText']))
                return
            
            # Filter out tool inputs/outputs
            filtered_outputs = []
            for output in analysis_outputs:
                if self._should_include_output(output):
                    filtered_outputs.append(output)
            
            if not filtered_outputs:
                story.append(Paragraph("No relevant analysis content available for PDF export.", self.styles['InfoText']))
                return
            
            # Group outputs by type
            outputs_by_type = {}
            for output in filtered_outputs:
                if output.type not in outputs_by_type:
                    outputs_by_type[output.type] = []
                outputs_by_type[output.type].append(output)
            
            for output_type, outputs in outputs_by_type.items():
                # Format output type title
                formatted_type = self._format_output_type_title(output_type)
                story.append(Paragraph(formatted_type, self.styles['SectionHeading']))
                
                for output in outputs:
                    if output.name and not self._is_tool_related_name(output.name):
                        story.append(Paragraph(f"<b>{output.name}</b>", self.styles['InfoText']))
                    
                    if output.text:
                        # Clean and format text content, filtering out tool-related content
                        text_content = self._clean_and_filter_text_for_pdf(output.text)
                        if text_content.strip():  # Only add if there's meaningful content after filtering
                            story.append(Paragraph(text_content, self.styles['Normal']))
                    
                    # Skip binary data that might be tool-related
                    if output.blob and not self._is_tool_related_blob(output):
                        story.append(Paragraph(f"[Binary data: {len(output.blob)} bytes]", self.styles['InfoText']))
                    
                    story.append(Spacer(1, 8))
            
            # Add analysis state summary but filter out tool-related data
            if market_analysis.state and isinstance(market_analysis.state, dict):
                filtered_state = self._filter_state_for_pdf(market_analysis.state)
                if filtered_state:
                    story.append(Paragraph("Analysis Summary", self.styles['SectionHeading']))
                    state_summary = self._format_state_summary(filtered_state)
                    story.append(Paragraph(state_summary, self.styles['CodeText']))
            
            story.append(Spacer(1, 20))
            
        except Exception as e:
            logger.error(f"Error adding expert content section: {e}", exc_info=True)
            story.append(Paragraph("Error generating expert content section", self.styles['Normal']))
    
    def _add_footer(self, story: List, market_analysis: MarketAnalysis):
        """Add PDF footer with generation info."""
        story.append(HRFlowable(width="100%", thickness=1, lineCap='round', color=HexColor('#E5E9F0')))
        story.append(Spacer(1, 12))
        
        generation_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        footer_text = f"Generated by BA2 Trade Platform on {generation_time}"
        story.append(Paragraph(footer_text, self.styles['InfoText']))
    
    # Helper methods
    
    def _get_instrument_details(self, symbol: str) -> Optional[Instrument]:
        """Get instrument details by symbol."""
        try:
            session = get_db()
            statement = select(Instrument).where(Instrument.name == symbol)
            instrument = session.exec(statement).first()
            session.close()
            return instrument
        except Exception as e:
            logger.error(f"Error loading instrument details for {symbol}: {e}", exc_info=True)
            return None
    
    def _format_datetime(self, dt: Optional[datetime]) -> str:
        """Format datetime for display."""
        if not dt:
            return "Unknown"
        
        # Convert UTC to local time for display
        if dt.tzinfo:
            local_time = dt.astimezone()
        else:
            local_time = dt.replace(tzinfo=timezone.utc).astimezone()
        
        return local_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    
    def _extract_error_message(self, state: Optional[Dict]) -> Optional[str]:
        """Extract error message from analysis state."""
        if not state or not isinstance(state, dict):
            return None
        
        # Look for direct error messages
        error_keys = ['error', 'exception', 'failure', 'failed']
        for key in error_keys:
            if key in state and state[key]:
                error_value = state[key]
                if isinstance(error_value, str):
                    return error_value
                elif isinstance(error_value, dict):
                    if 'message' in error_value:
                        return error_value['message']
                    elif 'error' in error_value:
                        return str(error_value['error'])
                    else:
                        return str(error_value)
                else:
                    return str(error_value)
        
        return None
    
    def _clean_text_for_pdf(self, text: str) -> str:
        """Clean text content for PDF formatting."""
        if not text:
            return ""
        
        # Remove or escape HTML-like tags that might cause issues
        text = text.replace('<', '&lt;').replace('>', '&gt;')
        
        # Limit line length to prevent formatting issues
        max_line_length = 80
        lines = text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            if len(line) <= max_line_length:
                cleaned_lines.append(line)
            else:
                # Break long lines
                words = line.split(' ')
                current_line = ''
                for word in words:
                    if len(current_line + ' ' + word) <= max_line_length:
                        current_line += (' ' + word if current_line else word)
                    else:
                        if current_line:
                            cleaned_lines.append(current_line)
                        current_line = word
                if current_line:
                    cleaned_lines.append(current_line)
        
        return '\n'.join(cleaned_lines)
    
    def _clean_and_filter_text_for_pdf(self, text: str) -> str:
        """Clean and filter text content for PDF, removing tool-related content."""
        if not text:
            return ""
        
        # First apply basic cleaning
        text = self._clean_text_for_pdf(text)
        
        # Filter out tool input/output sections
        lines = text.split('\n')
        filtered_lines = []
        skip_section = False
        
        tool_indicators = [
            'tool input:', 'tool output:', 'function call:', 'function result:',
            'api call:', 'api response:', '```json', '```python', '```xml',
            'input:', 'output:', 'result:', 'response:',
            'tool_calls:', 'function_calls:', 'tool_results:',
            '[tool:', '[function:', '[api:'
        ]
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Check if this line starts a tool section
            if any(indicator in line_lower for indicator in tool_indicators):
                skip_section = True
                continue
            
            # Check if this line ends a tool section (empty line or new section)
            if skip_section and (not line.strip() or line.startswith('#') or line.startswith('##')):
                skip_section = False
                if line.startswith('#'):  # Keep section headers
                    filtered_lines.append(line)
                continue
            
            # Skip lines in tool sections
            if skip_section:
                continue
            
            # Keep meaningful lines
            if line.strip():
                filtered_lines.append(line)
        
        return '\n'.join(filtered_lines)
    
    def _should_include_output(self, output: AnalysisOutput) -> bool:
        """Determine if an analysis output should be included in the PDF."""
        if not output:
            return False
        
        # Filter out tool-related outputs by type
        excluded_types = [
            'tool_input', 'tool_output', 'function_call', 'function_result',
            'api_call', 'api_response', 'debug', 'trace', 'log',
            'tool_calls', 'function_calls', 'tool_results'
        ]
        
        if output.type and output.type.lower() in excluded_types:
            return False
        
        # Filter out by name patterns
        if output.name:
            excluded_name_patterns = [
                'tool_', 'function_', 'api_', 'debug_', 'trace_',
                'input_', 'output_', 'result_', 'call_'
            ]
            name_lower = output.name.lower()
            if any(pattern in name_lower for pattern in excluded_name_patterns):
                return False
        
        return True
    
    def _is_tool_related_name(self, name: str) -> bool:
        """Check if a name is tool-related and should be excluded."""
        if not name:
            return False
        
        name_lower = name.lower()
        tool_patterns = [
            'tool_', 'function_', 'api_', 'debug_', 'trace_',
            'input_', 'output_', 'result_', 'call_', 'response_'
        ]
        
        return any(pattern in name_lower for pattern in tool_patterns)
    
    def _is_tool_related_blob(self, output: AnalysisOutput) -> bool:
        """Check if a blob output is tool-related and should be excluded."""
        if not output or not output.blob:
            return False
        
        # Check if the output type or name suggests it's tool-related
        return not self._should_include_output(output) or self._is_tool_related_name(output.name or '')
    
    def _format_output_type_title(self, output_type: str) -> str:
        """Format output type into a readable title."""
        if not output_type:
            return "Analysis Content"
        
        # Replace underscores and title case
        formatted = output_type.replace('_', ' ').title()
        
        # Special cases for better formatting
        replacements = {
            'Llm': 'LLM',
            'Api': 'API',
            'Ai': 'AI',
            'Ml': 'ML',
            'Pdf': 'PDF',
            'Html': 'HTML',
            'Json': 'JSON',
            'Xml': 'XML'
        }
        
        for old, new in replacements.items():
            formatted = formatted.replace(old, new)
        
        return formatted
    
    def _filter_state_for_pdf(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Filter analysis state to remove tool-related data."""
        if not state or not isinstance(state, dict):
            return {}
        
        filtered_state = {}
        excluded_keys = [
            'tool_calls', 'function_calls', 'tool_results', 'function_results',
            'api_calls', 'api_responses', 'debug', 'trace', 'logs',
            'input', 'output', 'calls', 'responses', 'errors'
        ]
        
        for key, value in state.items():
            key_lower = key.lower()
            
            # Skip tool-related keys
            if any(excluded in key_lower for excluded in excluded_keys):
                continue
            
            # For nested dictionaries, recursively filter
            if isinstance(value, dict):
                filtered_value = self._filter_state_for_pdf(value)
                if filtered_value:  # Only add if not empty after filtering
                    filtered_state[key] = filtered_value
            elif isinstance(value, list):
                # Filter lists but keep non-tool-related items
                filtered_list = []
                for item in value:
                    if isinstance(item, dict):
                        filtered_item = self._filter_state_for_pdf(item)
                        if filtered_item:
                            filtered_list.append(filtered_item)
                    elif not isinstance(item, str) or not any(excluded in str(item).lower() for excluded in excluded_keys):
                        filtered_list.append(item)
                if filtered_list:
                    filtered_state[key] = filtered_list
            else:
                # Keep non-tool-related simple values
                if not isinstance(value, str) or not any(excluded in str(value).lower() for excluded in excluded_keys):
                    filtered_state[key] = value
        
        return filtered_state
    
    def _format_state_summary(self, state: Dict[str, Any]) -> str:
        """Format analysis state for PDF display."""
        try:
            # Create a simplified summary of the state
            summary_parts = []
            
            for key, value in state.items():
                if key in ['error', 'exception', 'failure']:
                    continue  # Already handled in error section
                
                if isinstance(value, dict):
                    summary_parts.append(f"{key}: {len(value)} items")
                elif isinstance(value, list):
                    summary_parts.append(f"{key}: {len(value)} entries")
                elif isinstance(value, str) and len(value) > 100:
                    summary_parts.append(f"{key}: {value[:100]}...")
                else:
                    summary_parts.append(f"{key}: {value}")
            
            return '\n'.join(summary_parts[:10])  # Limit to first 10 items
            
        except Exception as e:
            logger.error(f"Error formatting state summary: {e}", exc_info=True)
            return "State summary unavailable"


# Convenience function for UI integration
def export_market_analysis_pdf(analysis_id: int, output_path: Optional[str] = None) -> str:
    """
    Convenience function to export a market analysis to PDF.
    
    Args:
        analysis_id: The ID of the MarketAnalysis to export
        output_path: Optional path for the output file
        
    Returns:
        The path to the generated PDF file
    """
    exporter = MarketAnalysisPDFExport()
    return exporter.export_analysis_to_pdf(analysis_id, output_path)