import lamindb as ln
import pandas as pd
import numpy as np

# Track the run against your benchmarking project
ln.track(project="1000 Genomes")

@ln.step()
def load_cnv_data() -> pd.DataFrame:
    """Retrieves and loads the targeted artifact from LaminDB."""
    schema = ln.Schema.get(name="1000 Genomes CNV VCF")
    projects = ln.Project.lookup()
    
    # Grabbing the first matched artifact for the benchmark
    artifacts = ln.Artifact.filter(schema=schema, projects=projects.ln_1000_genomes).order_by("created_at")
    
    # Loading directly into memory for vectorized operations
    return artifacts[0].load()

@ln.step()
def compute_sample_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates summary statistics using grouped, vectorized operations."""
    
    # 1. Create boolean masks for our conditions (much faster than row-by-row checks)
    df['is_deletion'] = df['INFO_SVLEN'] < 0
    df['is_homozygous'] = df['SAMPLE_GT'] == '1/1'
    df['is_heterozygous'] = df['SAMPLE_GT'] == '0/1'
    
    if 'SAMPLE_SM' in df.columns:
        df['is_high_conf'] = abs(df['SAMPLE_SM'] - 1.0) > 0.5
    else:
        df['is_high_conf'] = False
        
    # Isolate deletion sizes for easier aggregation
    df['del_size'] = np.where(df['is_deletion'], df['INFO_SVLEN'].abs(), np.nan)

    # 2. Group by sample and aggregate in a single pass
    stats = df.groupby('SAMPLE_NAME').agg(
        Total_CNVs=('INFO_SVLEN', 'count'),
        Deletions=('is_deletion', 'sum'),
        Homozygous_CNVs=('is_homozygous', 'sum'),
        Heterozygous_CNVs=('is_heterozygous', 'sum'),
        High_Confidence_CNVs=('is_high_conf', 'sum'),
        Min_Deletion_Size=('del_size', 'min'),
        Max_Deletion_Size=('del_size', 'max'),
        Median_Deletion_Size=('del_size', 'median')
    ).reset_index()
    
    # 3. Clean up the final dataframe
    stats['Duplications'] = stats['Total_CNVs'] - stats['Deletions']
    stats = stats.fillna(0) # Handle samples with no deletions
    
    return stats

@ln.flow()
def run_benchmark_pipeline():
    """Main pipeline execution wrapped for Lamin tracking."""
    
    # Execute the steps
    df = load_cnv_data()
    stats_df = compute_sample_stats(df)
    
    # Optionally save the result back to LaminDB as a tracked artifact
    output_artifact = ln.Artifact(stats_df, description="Vectorized sample-level CNV statistics")
    output_artifact.save()
    
    print(f"Processed {len(stats_df)} samples successfully.")
    return output_artifact

if __name__ == "__main__":
    run_benchmark_pipeline()